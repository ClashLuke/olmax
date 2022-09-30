import typing

import jax
from jax import lax, numpy as jnp

from src.backend import device_id, get_param, matmul, promote_to, with_context
from src.constants import ParallelAxes
from src.context import Context
from src.model.activate import activate
from src.model.conv import conv
from src.model.norm import prenorm, scale_norm_act


def z_loss(ctx: Context, src: jnp.ndarray, use_previous_grad: bool = True) -> jnp.ndarray:
    # forward: 0 (-> to not change loss)
    # backward: grad(jnp.square(log_z).mean() * ctx.training.z_loss)
    @jax.custom_gradient
    def _fn(inp: jnp.ndarray):
        def _grad(dy):
            grad = ctx.training.z_loss / inp.size
            if use_previous_grad:
                grad = grad * dy
            return inp * grad

        return jnp.zeros((), dtype=inp.dtype), _grad

    return _fn(src)


def one_hot(inp: jnp.ndarray, size: int) -> jnp.ndarray:
    return jnp.equal(jnp.reshape(inp, inp.shape + (1,)), jnp.reshape(jnp.arange(0, size), (1,) * inp.ndim + (size,)))


def top1_gating(ctx: Context, gate: jnp.ndarray, x: jnp.ndarray) -> typing.Tuple[jnp.ndarray, jnp.ndarray]:
    # prepare shapes
    batch, sequence, experts = gate.shape
    features = x.shape[-1]
    tokens = batch * sequence
    overflow = tokens // experts
    dtype = x.dtype
    gate = promote_to(gate, jnp.float32)
    x = promote_to(x, jnp.float32)
    gate = gate.reshape(batch * sequence, experts)

    # parallel-softmax gate
    max_gate = lax.pmax(lax.stop_gradient(gate), ParallelAxes.model)
    lse = lax.psum(jnp.exp(gate - max_gate), ParallelAxes.model) + max_gate
    lse += z_loss(ctx, lse, False)  # actual zloss
    gate = jnp.exp(gate)
    gate += z_loss(ctx, gate, False)  # aux loss
    balanced = gate / lax.stop_gradient(gate).sum(0, keepdims=True)  # balance gates across batch

    # shuffle to avoid imbalances across token position (https://arxiv.org/abs/2109.10465)
    ctx.prng_key, key = jax.random.split(ctx.prng_key)
    indices = jnp.argsort(jax.random.normal(key, (gate.shape[0],)), 0)
    balanced = jnp.take_along_axis(balanced, jnp.broadcast_to(indices.reshape(-1, 1), gate.shape), 0)

    # avoid overflow / get best index
    assignments = jnp.argsort(balanced, -1)
    square_hot = one_hot(assignments, features)
    mask = (square_hot.cumsum(0) > overflow).cumsum(2) < 1
    square_hot = jnp.bitwise_and(square_hot, mask)
    mask = square_hot.sum(-1)
    mask = mask * experts
    assignments = jnp.argsort(assignments, -1)
    assignments = assignments - mask
    assignments = jnp.argmax(assignments, -1)

    # unshuffle
    indices = jnp.argsort(indices)
    assignments = jnp.take_along_axis(assignments, indices, 0)

    # get slice of tokens
    index = device_id()
    own_indices = jnp.argsort(assignments == index)[-overflow:]
    weight = jnp.take_along_axis(gate, assignments.reshape(*assignments.shape, 1), -1)
    weight = jnp.take_along_axis(weight, own_indices.reshape(-1, 1), 0)
    x = x.reshape(batch * sequence, features)
    x = jnp.take_along_axis(x, jnp.broadcast_to(own_indices.reshape(-1, 1), (overflow, features)), 0)
    x = x * weight
    x = x.astype(dtype)

    return x, own_indices


@prenorm
@with_context()
def moe(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    inp_wgt = get_param(ctx, "ff_input", [ctx.dims.features, ctx.dims.moe_intermediate])
    out_wgt = get_param(ctx, "ff_output", [ctx.dims.moe_intermediate, ctx.dims.features])

    gates = conv(ctx, inp, ctx.dims.pointwise_kernel, ctx.dims.features, ctx.dims.features)
    mid, indices = top1_gating(ctx, gates, inp)
    mid = matmul(mid, inp_wgt)
    mid = activate(mid)
    out = matmul(mid, out_wgt)
    return jnp.zeros_like(inp).reshape(-1, inp.shape[-1]).at[indices].set(out).reshape(inp.shape)


def all_to_all(ctx: Context, x: jnp.ndarray, split_axis: int, concat_axis: int) -> jnp.ndarray:
    if ctx.is_initializing:
        return x

    @jax.custom_gradient
    def _fn(inp: jnp.ndarray):
        def _grad(dy: jnp.ndarray) -> jnp.ndarray:
            return lax.all_to_all(dy, ParallelAxes.model, concat_axis, split_axis, tiled=True)

        return lax.all_to_all(inp, ParallelAxes.model, split_axis, concat_axis, tiled=True), _grad

    return _fn(x)


@prenorm
@with_context()
def dense_moe(ctx: Context, inp: jnp.ndarray) -> jnp.ndarray:
    devices = ctx.dims.heads
    big_params = devices * ctx.dims.inner_bottleneck_features
    sequence_slice = ctx.dims.sequence // devices

    inp = conv(ctx, inp, ctx.dims.outer_bottleneck_kernel, ctx.dims.features, ctx.dims.inner_bottleneck_features)

    # [Batch, Sequence, Features]  ->  [Batch, SequenceSlice, Features * Devices]
    # In essence, 1) Collect features from all devices + 2) Drop unused sequence elements
    if not ctx.is_initializing:
        inp = inp.reshape(ctx.dims.batch, sequence_slice, devices, ctx.dims.inner_bottleneck_features)
        inp = all_to_all(ctx, inp, 2, 3)
        inp = inp.reshape(ctx.dims.batch, sequence_slice, big_params)

    # Devices^2 more parameters than normal bottleneck block but only Devices-times more flops due to sparsity above
    inp = scale_norm_act(ctx, inp, big_params)
    inp = conv(ctx, inp, ctx.dims.inner_bottleneck_kernel, big_params, big_params)
    inp = scale_norm_act(ctx, inp, big_params)

    # [Batch, SequenceSlice, Features * Devices]  ->  [Batch, Sequence, Features]  (PixelShuffle across devices)
    if not ctx.is_initializing:
        inp = inp.reshape(ctx.dims.batch, sequence_slice, 1, big_params)
        inp = all_to_all(ctx, inp, 3, 2)
        inp = inp.reshape(ctx.dims.batch, ctx.dims.sequence, ctx.dims.inner_bottleneck_features)

    return conv(ctx, inp, ctx.dims.outer_bottleneck_kernel, ctx.dims.inner_bottleneck_features, ctx.dims.features)
