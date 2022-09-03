import copy
import typing

import jax
from jax import numpy as jnp

from src.context import Context

REVERSIBLE_CTX = typing.Tuple[typing.Dict[str, jnp.ndarray], jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]
ReversibleFn = typing.Callable[[Context, jnp.ndarray], jnp.ndarray]
FourArrays = typing.Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]


def reversible(ctx: Context, fn: ReversibleFn, src: REVERSIBLE_CTX, *args) -> REVERSIBLE_CTX:
    if ctx.is_initializing:
        params, _x00, x01, x10, x11 = src
        new_ctx = ctx.add_to_prefix("reversible")
        new_ctx.parameters = params
        out = fn(new_ctx, x10, *args)
        ctx.parameters = new_ctx.parameters
        ctx.name_cache = new_ctx.name_cache
        ctx.prng_key = new_ctx.prng_key
        return new_ctx.parameters, x10, x11, out, x01

    name_cache = copy.deepcopy(ctx.name_cache)

    def base(params: typing.Dict[str, jnp.ndarray], inp: jnp.ndarray, *inner_args) -> jnp.ndarray:
        ctx.name_cache = copy.deepcopy(name_cache)
        new_ctx = ctx.add_to_prefix("reversible")
        new_ctx.parameters = params
        out = fn(new_ctx, inp, *inner_args)
        ctx.name_cache = new_ctx.name_cache
        return out

    @jax.custom_gradient
    def _fn(params: typing.Dict[str, jnp.ndarray], x0: jnp.ndarray, _back_x0: jnp.ndarray, x1: jnp.ndarray,
            _back_x1: jnp.ndarray, *inner_args):
        def _grad(dy):
            d_params_old, dy0, y0, dy1, y1 = dy
            x0, grad_fn = jax.vjp(base, params, y0, *inner_args)
            d_params, dx0, *_ = grad_fn(dy1)
            d_params = {k: d_params_old.get(k, 0) + d_params.get(k, 0) for k in d_params.keys()}
            return (d_params, dy1, y1 - x0, dx0 + dy0, y0) + (None,) * len(inner_args)

        out = base(params, x1, *inner_args) + x0
        return (params, x1, x1, out, out), _grad

    return _fn(*src, *args)


def revnet_out(src: FourArrays) -> jnp.ndarray:
    @jax.custom_gradient
    def _fn(x0: jnp.ndarray, _x0_back: jnp.ndarray, x1: jnp.ndarray, _x1_back: jnp.ndarray):
        def _grad(dy) -> FourArrays:
            return dy, x0, dy, x1

        return x0 + x1, _grad

    return _fn(*src)
