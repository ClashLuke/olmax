from typing import Tuple

import jax
from jax import numpy as jnp, lax

from src.backend import matmul, get_param, square_grad, with_context
from src.constants import ParallelAxes
from src.context import Context
from src.model.norm import scale_norm_act, scale_norm_act_linear


@with_context()
def linear(ctx: Context, inp: jax.Array, in_features: int, out_features: int):
    weight, weight_sq = get_param(ctx, "conv_weight", [out_features, in_features], column_axes=2, return_sq=True)
    if ctx.is_initializing:
        return jnp.zeros(inp.shape[:-1] + (out_features,), dtype=inp.dtype)

    def _mm(x, y):
        return matmul(x, y)

    return square_grad(_mm, inp, weight, weight_sq)


@jax.custom_gradient
def all2all(inp):
    def _grad(dy):
        return lax.all_to_all(dy, ParallelAxes.model, inp.ndim - 1, inp.ndim - 1, tiled=True)

    return lax.all_to_all(inp, ParallelAxes.model, inp.ndim - 1, inp.ndim - 1, tiled=True), _grad


@with_context()
def input_embed(ctx: Context, inp: jax.Array, dim: int) -> jax.Array:
    param, param_sq = get_param(ctx, "inp_embd", [dim, ctx.dims.pointwise_features],
                                std=1 / ctx.dims.pointwise_features, return_sq=True)

    def _fn(src, wgt):
        return jnp.take(wgt, src, 0)

    if ctx.is_initializing:
        return _fn(inp, param)

    return square_grad(_fn, inp, param, param_sq)


@with_context()
def pos_and_scale(ctx: Context, inp: jax.Array) -> Tuple[jax.Array, jax.Array]:
    # from https://github.com/HomebrewNLP/HomebrewNLP-MTF/blob/v1.16.0/src/model/basic.py#L93
    gate_sqrt = int(ctx.dims.memory_slots ** 0.5)
    assert gate_sqrt ** 2 == ctx.dims.memory_slots

    gates = linear(ctx, inp, ctx.dims.pointwise_features, gate_sqrt * 2 * ctx.dims.memory_read_heads)

    gates = gates.reshape(ctx.dims.batch, ctx.dims.memory_read_heads, 2, gate_sqrt)
    gates = scale_norm_act(ctx, gates, gate_sqrt, act=False)
    gates -= lax.stop_gradient(gates.max(-1).sum(-1))
    gates = lax.exp(gates)
    denominator = lax.reciprocal(gates.sum(-1)).prod(-1)
    values, idx = lax.top_k(gates, ctx.dims.memory_slots_read_per_head)  # along last axis
    idx = jnp.einsum("bhpk,p->bhk", idx, jnp.array([1, gate_sqrt]))
    values = values.prod(-2) * denominator
    # [Batch Slots MemoryFeatures] [Batch Heads TopK] -> [Batch, Heads * TopK, MemoryFeatures]
    return idx.reshape(ctx.dims.batch, -1, 1), values.reshape(ctx.dims.batch, -1, 1)


@with_context
def input_fn(ctx: Context, token: jax.Array, position: jax.Array, dense: jax.Array, output_features: int
             ) -> Tuple[jax.Array, jax.Array, jax.Array]:
    token_embedding = input_embed(ctx, token, ctx.dims.vocab)
    position_embedding = input_embed(ctx, position, ctx.dims.sequence)
    dense = linear(ctx, dense, ctx.dims.features, ctx.dims.pointwise_features)
    inp = scale_norm_act(ctx, token_embedding + position_embedding + dense, ctx.dims.pointwise_features)
    offset0 = linear(ctx, inp, ctx.dims.pointwise_features, output_features)
    offset1 = linear(ctx, all2all(inp), ctx.dims.pointwise_features, ctx.dims.features)
    return offset0, offset1, inp


@with_context()
def read(ctx: Context, token: jax.Array, position: jax.Array, dense0: jax.Array, sparse: jax.Array) -> jax.Array:
    total_read = ctx.dims.memory_features * ctx.dims.memory_read_heads * ctx.dims.memory_slots_read_per_head

    offset0, offset1, inp = input_fn(ctx, token, position, dense0, total_read)
    idx, val = pos_and_scale(ctx, inp)
    inp = (jnp.take_along_axis(sparse, idx, 1) * val).reshape(ctx.dims.batch, total_read)

    inp0 = scale_norm_act_linear(ctx, inp + offset0, total_read, ctx.dims.features)
    inp1 = scale_norm_act_linear(ctx, inp, total_read, ctx.dims.features, act=False)

    return offset1 + inp0 + inp1


@with_context()
def write(ctx: Context, token: jax.Array, position: jax.Array, dense1: jax.Array
          ) -> Tuple[jax.Array, jax.Array, jax.Array]:
    total_read = ctx.dims.memory_features * ctx.dims.memory_read_heads * ctx.dims.memory_slots_read_per_head

    dense_parallel = scale_norm_act_linear(ctx, dense1, ctx.dims.features, ctx.dims.pointwise_features, act=False)
    offset0, offset1, _ = input_fn(ctx, token, position, dense1, ctx.dims.pointwise_features)

    inp = scale_norm_act(ctx, dense_parallel + offset0 + offset1, ctx.dims.pointwise_features)

    dense0 = linear(ctx, inp, ctx.dims.pointwise_features, ctx.dims.features)
    scatter_values = linear(ctx, inp, ctx.dims.pointwise_features, total_read)
    idx, val = pos_and_scale(ctx, inp)

    return dense0, idx, scatter_values.reshape(ctx.dims.batch, -1, ctx.dims.memory_features) * val
