"""
Adapted from https://github.com/kingoflolz/mesh-transformer-jax/blob/0a75ca9370576ad9d247facf6cb8e9699300e690
/mesh_transformer/checkpoint.py
"""
import datetime
import functools
import io
import json
import multiprocessing
import re
import time
import traceback
import typing

import jax
import jax.numpy as jnp
import numpy as np
from jax import lax
from jax.tree_util import PyTreeDef
from smart_open import open as smart_open

from src.backend import deep_replace, is_main
from src.context import Context, WhileTrainContext

UPLOAD_RETRIES = 8


def index_weights(weights, idx):
    cpu_device = jax.devices("cpu")[0]
    return jax.device_put(jax.tree_util.tree_map(lambda i: i[idx], weights), cpu_device)


def write(weights: typing.List[jnp.ndarray], file_path: str):
    for _ in range(UPLOAD_RETRIES):
        try:
            with smart_open(file_path, "wb") as f:
                np.savez(f, **{str(idx): tensor for idx, tensor in enumerate(weights)})
            return
        except:  # skipcq: FLK-E722
            print("save failed, trying again")

    print("save failed 3 times, exiting")
    raise Exception("save failed")


def log(arg: str, verbose: bool):
    if verbose:
        print(datetime.datetime.now(), arg)


def device_ids():
    def _inner(_):
        aggregated = lax.psum_scatter(jnp.arange(jax.device_count()), scatter_dimension=0, axis_name='i')
        return (aggregated // jax.device_count()).astype(jnp.int32)

    return jax.pmap(_inner, 'i')(jnp.arange(jax.local_device_count())).tolist()


def write_checkpoint(ctx: Context, verbose: bool = True):
    flattened, jax_structure = jax.tree_util.tree_flatten(ctx.parameters)
    variance, _ = jax.tree_util.tree_flatten(ctx.parameter_variance)  # same structure

    structure = str(jax_structure)  # like "PyTreeDef({'2': {'a': *}})"
    structure = structure.replace('PyTreeDef', '')[1:-1]  # clean up "types"
    structure = structure.replace(': *', ': null').replace("{'", '{"').replace("':", '":')
    structure = structure.replace("', ", '", ').replace(", '", ', "')  # to valid JSON

    if is_main():
        log(f"Writing structure to {ctx.training.checkpoint_path}/structure.json", verbose)
        success = False
        for _ in range(UPLOAD_RETRIES):
            try:
                with smart_open(f"{ctx.training.checkpoint_path}/structure.json", "w") as f:  # skipcq: PTC-W6004
                    f.write(structure)
            except:  # skipcq: FLK-E722
                print("Failed to save structure. Traceback:")
                traceback.print_exc()
                continue
            success = True
            break
        if not success:
            raise ValueError("Couldn't save structure")

    for shard in device_ids():
        log(f"Uploading {shard=} to {ctx.training.checkpoint_path}/{shard}/", verbose)
        for tree, suffix in ((flattened, "parameters"), (variance, "variance")):
            local_weights = index_weights(tree, shard)
            write(local_weights, f"{ctx.training.checkpoint_path}/{shard}/{suffix}.npz")


def write_train_checkpoint(wctx: WhileTrainContext, verbose: bool = True):
    write_checkpoint(wctx.ctx, verbose)
    for shard in device_ids():
        for tree, suffix in ((wctx.loss, "loss"), (wctx.accuracy, "accuracy"), (wctx.current_step, "current_step")):
            write(index_weights([tree], shard), f"{wctx.ctx.training.checkpoint_path}/{shard}/{suffix}.npz")


def read_shard(checkpoint_dir):
    with smart_open(checkpoint_dir, "rb") as f:
        buf = f.read()
    f_io = io.BytesIO(buf)
    deserialized = list(np.load(f_io).items())
    return [tensor for idx, tensor in sorted(deserialized, key=lambda x: int(x[0]))]


def unshard(shards):
    unsharded = []
    for all_shards in zip(*shards):
        x = np.stack(all_shards)
        if x.dtype == np.dtype('V2'):
            x.dtype = jnp.bfloat16
        unsharded.append(x)  # manual jnp.asarray -> replicated; automatic (via jax.pmap) -> parallel (as before)
    return unsharded


def _read_shards(path: str, structure: PyTreeDef, suffix: str):
    with multiprocessing.pool.ThreadPool(jax.local_device_count()) as p:
        start = time.time()
        paths = [f"{path}/{shard}/{suffix}.npz" for shard in device_ids()]
        shards = list(p.map(read_shard, paths))
        print(f"Loading {suffix} took {time.time() - start:.2f}s")

    return structure.unflatten(unshard(shards))


def _overwrite(new: dict, old: dict, ignore: re.Pattern):
    if not old:
        print("No entries in old dict. Using new dict.")
        for key, param in new.items():
            old[key] = param
        return

    print("Unknown:  ", [p for p in new.keys() if p not in old and not ignore.match(p)])
    print("Unfilled: ", [p for p in old.keys() if p not in new and not ignore.match(p)])

    for key in old.keys():
        if key in new:
            old[key] = new[key]


def read_checkpoint(ctx: Context, ignore: str = '.*optimizer.*', load_variance: bool = False):
    ignore = re.compile(ignore)

    with smart_open(f"{ctx.training.checkpoint_load_path}/structure.json", "r") as f:
        structure = f.read()
    structure = json.loads(structure)
    py_structure = deep_replace(structure, jnp.zeros((1,)))
    _, structure = jax.tree_util.tree_flatten(py_structure)

    _overwrite(_read_shards(ctx.training.checkpoint_load_path, structure, "parameters"), ctx.parameters, ignore)

    if load_variance:
        py_structure = {k: v for k, v in py_structure.items() if "optimizer" not in k}  # no optimizer for param-lr
        _, structure = jax.tree_util.tree_flatten(py_structure)
        _overwrite(_read_shards(ctx.training.checkpoint_load_path, structure, "variance"), ctx.parameter_variance,
                   ignore)


def read_train_checkpoint(wctx: WhileTrainContext, ignore: str = '.*optimizer.*'):
    _, structure = jax.tree_util.tree_flatten([jnp.zeros((1,))])
    wctx.loss = _read_shards(wctx.ctx.training.checkpoint_load_path, structure, "loss")[0]
    wctx.accuracy = _read_shards(wctx.ctx.training.checkpoint_load_path, structure, "accuracy")[0]
    wctx.current_step = _read_shards(wctx.ctx.training.checkpoint_load_path, structure, "current_step")[0]
    read_checkpoint(wctx.ctx, ignore, load_variance=True)
