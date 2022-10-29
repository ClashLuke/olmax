import math

from jax import numpy as jnp

from src.backend import dot, get_param, pattern_match, with_context
from src.context import Context
from src.model.norm import prenorm, scale_norm_act


@prenorm
@with_context()
def mix(ctx: Context, inp: jnp.ndarray, depth: jnp.ndarray) -> jnp.ndarray:
    weight_shape = [ctx.dims.spatial_mixing_kernel] * 2
    wgt0 = get_param(ctx, "mix_0", weight_shape)
    wgt1 = get_param(ctx, "mix_1", weight_shape)
    scale = get_param(ctx, "scale", [ctx.dims.features], std=0, mean=1,
                      dtype=jnp.promote_types(ctx.model.computation_dtype, jnp.float32))
    if ctx.is_initializing:
        return inp

    original_shape = inp.shape
    _batch, sequence, _features = original_shape
    max_dims = math.ceil(math.log(sequence, ctx.dims.spatial_mixing_kernel))
    original_batch = inp.shape[0]
    if ctx.model.autoregressive:
        wgt0 = jnp.triu(wgt0)
        wgt1 = jnp.triu(wgt1)

    def _get_mix_fn(current_depth: int):
        def _fn(x: jnp.ndarray):
            batch = max(sequence // ctx.dims.spatial_mixing_kernel ** (current_depth % max_dims + 1), 1)
            out = x.reshape(original_batch * batch, ctx.dims.spatial_mixing_kernel, -1)
            inner_batch, inner_sequence, inner_features = out.shape

            # Shape[Batch, Sequence, Features] * Shape[Sequence, Sequence] -> Shape[Batch, Features, Sequence]
            out = dot(out, wgt0, left_contract_dims=(1,), right_contract_dims=(0,))

            out = out.reshape(-1, ctx.dims.features, inner_sequence)
            out = scale_norm_act(ctx, out, ctx.dims.features, weight=scale, add_to_prefix=False, dim=1)
            out = out.reshape(inner_batch, inner_features, inner_sequence)

            # Shape[Batch, Features, Sequence] * Shape[Sequence, Sequence] -> Shape[Batch, Features, Sequence]
            out = dot(out, wgt1, left_contract_dims=(2,), right_contract_dims=(0,))
            out = out.transpose(0, 2, 1)
            return out.reshape(original_shape)

        return _fn

    return pattern_match(_get_mix_fn, max_dims, depth, inp)
