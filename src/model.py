import copy
import typing

import jax
from jax import lax, numpy as jnp

from src.backend import get_param, INT_OR_TUPLE, dot, matmul, conv, sum_pool
from src.constants import ParallelAxes
from src.context import Context

REVERSIBLE_CTX = typing.Tuple[typing.Dict[str, jnp.ndarray], jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]


def activate(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    if ctx.is_initializing:
        return inp
    return jax.nn.leaky_relu(inp, ctx.model.leaky_relu_slope)


def norm(ctx: Context, inp: jnp.ndarray, dims: INT_OR_TUPLE, keepdims=False) -> jnp.ndarray:
    square = jnp.square(inp).sum(dims, keepdims=keepdims)
    return lax.rsqrt(ctx.model.norm_eps + square)


def psum(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    if ctx.is_initializing:
        return inp
    return lax.psum(inp, ParallelAxes.model)


def normalize(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("normalization")
    scale = get_param(ctx, "scale", [ctx.dims.heads, ctx.dims.one], std=0)

    @jax.custom_gradient
    def _fn(src: jnp.ndarray):
        mean = src.mean(-1, keepdims=True)
        out = src - mean
        scale = norm(ctx, out, -1, True) * src.shape[-1] ** 0.5
        out = out * scale

        def _grad(dy: jnp.ndarray) -> jnp.ndarray:
            dy = dy * scale
            dy -= (dy * out).mean(-1, keepdims=True) * out
            dy -= dy.mean(-1, keepdims=True)
            return dy

        return out, _grad

    return _fn(inp) * (1 + scale)


def pool_heads(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    return sum_pool(inp, [0, ctx.model.device_halo_size], [(0, 0), (ctx.model.device_halo_size // 2,) * 2])


def conv_weight(ctx: Context, inp: jnp.ndarray, depthwise: bool, conv_kernel: str, scale: float):
    weight = get_param(ctx, "weight", [ctx.dims.heads, ctx.dims.features_per_head,
                                       ctx.dims.one if depthwise else ctx.dims.features_per_head, conv_kernel],
                       column_axes=2, scale=scale, split_dims=[ctx.dims.depth, ctx.dims.heads])
    if ctx.is_initializing:
        return inp
    return conv(inp, weight, [(weight.shape[-1] - 1, 0)], ctx.dims.sizes.features_per_head if depthwise else 1)


def mm(ctx: Context, inp: jnp.ndarray, weight: jnp.ndarray):
    if ctx.is_initializing:
        return inp
    return dot(inp, weight, -1, 0, (), ())


def full_conv(ctx: Context, inp: jnp.ndarray, scale: float) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("full_conv")
    return conv_weight(ctx, inp, False, ctx.dims.full_conv_kernel, scale)


def depthwise_conv(ctx: Context, inp: jnp.ndarray, scale: float) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("depthwise_conv")
    return conv_weight(ctx, inp, True, ctx.dims.depthwise_conv_kernel, scale)


def rezero(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("rezero")
    scale = get_param(ctx, "scale", [ctx.dims.heads, ctx.dims.one], std=0,
                      learning_rate_scale=ctx.model.rezero_learning_rate_scale)
    return inp * scale


def conv_block(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("group_convolution")

    inp = normalize(ctx, inp)
    mid = depthwise_conv(ctx, inp, 1 / ctx.model.activation_std)
    mid = activate(ctx, mid)
    mid = normalize(ctx, mid)
    return full_conv(ctx, mid, ctx.dims.sizes.depth ** -0.5)


def feed_forward_features(ctx: Context, in_dim: str, out_dim: str) -> typing.Tuple[
    jnp.ndarray, jnp.ndarray]:
    inp_weight = get_param(ctx, "inp_weight", [ctx.dims.heads, in_dim, out_dim], scale=1 / ctx.model.activation_std)
    out_weight = get_param(ctx, "out_weight", [out_dim, ctx.dims.heads, in_dim], scale=ctx.dims.sizes.depth ** -0.5,
                           column_axes=2)
    return inp_weight, out_weight


def group_feed_forward(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("group_feed_forward")
    inp_weight, out_weight = feed_forward_features(ctx, ctx.dims.features_per_head, ctx.dims.intermediate_parallel)

    inp = normalize(ctx, inp)
    mid = mm(ctx, inp, inp_weight)
    mid = activate(ctx, mid)
    mid = normalize(ctx, mid)
    out = mm(ctx, mid, out_weight)
    return out


def feed_forward(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("feed_forward")
    inp_weight, out_weight = feed_forward_features(ctx, ctx.dims.features_per_head, ctx.dims.intermediate_replicated)

    inp = normalize(ctx, inp)
    mid = mm(ctx, inp, inp_weight)
    mid = psum(ctx, mid)
    mid = activate(ctx, mid)
    mid = normalize(ctx, mid)
    out = mm(ctx, mid, out_weight)
    return out


def one_hot(inp: jnp.ndarray, size: int) -> jnp.ndarray:
    return jnp.equal(jnp.reshape(inp, inp.shape + (1,)), jnp.reshape(jnp.arange(0, size), (1,) * inp.ndim + (size,)))


def input_embed(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("input_embed")
    inp_embd = get_param(ctx, "inp_embd", [ctx.dims.vocab, ctx.dims.heads, ctx.dims.features_per_head], std=1e-5)
    out = jnp.take(inp_embd, inp, 0)
    return normalize(ctx, out)


def output_embed_shard(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    ctx = ctx.add_to_prefix("output_embed")
    embd = get_param(ctx, "weight", [ctx.dims.heads, ctx.dims.features_per_head, ctx.dims.vocab], std=0,
                     learning_rate_scale=1 / (ctx.dims.sizes.heads * ctx.dims.sizes.features_per_head))
    if ctx.is_initializing:
        return inp
    return matmul(inp, embd)


def reversible(ctx: Context, fn: typing.Callable[[Context, jnp.ndarray], jnp.ndarray], src: REVERSIBLE_CTX
               ) -> REVERSIBLE_CTX:
    if ctx.is_initializing:
        params, x00, x01, x10, x11 = src
        new_ctx = ctx.add_to_prefix("reversible")
        new_ctx.parameters = params
        out = fn(new_ctx, x10)
        ctx.parameters = new_ctx.parameters
        ctx.parameter_dims = new_ctx.parameter_dims
        ctx.name_cache = new_ctx.name_cache
        ctx.prng_key = new_ctx.prng_key
        return new_ctx.parameters, x10, x11, out, x01

    name_cache = copy.deepcopy(ctx.name_cache)

    def base(params: typing.Dict[str, jnp.ndarray], inp: jnp.ndarray) -> jnp.ndarray:
        ctx.name_cache = copy.deepcopy(name_cache)
        new_ctx = ctx.add_to_prefix("reversible")
        new_ctx.parameters = params
        out = fn(new_ctx, inp)
        ctx.name_cache = new_ctx.name_cache
        return out

    @jax.custom_gradient
    def _fn(params: typing.Dict[str, jnp.ndarray], x0: jnp.ndarray, back_x0: jnp.ndarray, x1: jnp.ndarray,
            back_x1: jnp.ndarray):
        def _grad(dy: REVERSIBLE_CTX) -> typing.Tuple[typing.Dict[str, jnp.ndarray], jnp.ndarray, jnp.ndarray,
                                                      jnp.ndarray, jnp.ndarray]:
            d_params_old, dy0, y0, dy1, y1 = dy
            x0, grad_fn = jax.vjp(base, params, y0)
            d_params, dx0, _ = grad_fn(dy1)
            d_params = {k: d_params_old.get(k, 0) + d_params.get(k, 0) for k in d_params.keys()}
            return d_params, dy1, y1 - x0, dx0 + dy0, y0

        out = base(params, x1) + x0
        return (params, x1, x1, out, out), _grad

    return _fn(*src)


def cross_entropy_loss(ctx: Context, src: jnp.ndarray, tgt: jnp.ndarray) -> typing.Tuple[jnp.ndarray, jnp.ndarray]:
    src = psum(ctx, src)  # TODO: Split batch across model parallel
    max_logit = lax.stop_gradient(src).max(-1, keepdims=True)
    log_z = lax.log(lax.exp(src - max_logit).sum(-1, keepdims=True)) + max_logit
    loss = log_z - jnp.take_along_axis(src, tgt.reshape(*tgt.shape, 1), -1)
    loss = loss.mean()
    accuracy = (jnp.argmax(src, 2) == tgt).astype(jnp.float32).mean()
    if ctx.training.z_loss:
        loss += jnp.square(log_z).mean() * ctx.training.z_loss
    return loss, accuracy


def revnet_out(src: typing.Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]) -> jnp.ndarray:
    @jax.custom_gradient
    def _fn(x0: jnp.ndarray, x0_back: jnp.ndarray, x1: jnp.ndarray, x1_back: jnp.ndarray):
        def _grad(dy) -> typing.Tuple[jnp.ndarray, jnp.ndarray, None, jnp.ndarray]:
            return dy, x0, dy, x1

        return x0 + x1, _grad

    return _fn(*src)


def body_ctx(ctx: Context, src: jnp.ndarray) -> typing.Union[typing.Tuple[jnp.ndarray, jnp.ndarray], jnp.ndarray]:
    src = input_embed(ctx, src)
    zero = jnp.zeros_like(src)
    src = (ctx.parameters, src, zero, src, zero)
    for i in range(ctx.dims.sizes.depth):
        src = reversible(ctx, conv_block, src)
        src = reversible(ctx, feed_forward, src)
    ctx.parameters = src[0]
    return output_embed_shard(ctx, revnet_out(src[1:]))


def compute(params: typing.Dict[str, jnp.ndarray], inp: jnp.ndarray) -> typing.Tuple[jnp.ndarray, jnp.ndarray]:
    ctx = Context()
    ctx.parameters = params
    src, tgt = inp
    return cross_entropy_loss(ctx, body_ctx(ctx, src), tgt)
