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
from smart_open import open as smart_open

from src.backend import is_main, deep_replace
from src.context import Context, WhileTrainContext

UPLOAD_RETRIES = 8


@functools.partial(jax.jit, backend="cpu")
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


def write_checkpoint(ctx: Context, verbose: bool = True):
    flattened, structure = jax.tree_util.tree_flatten(ctx.parameters)
    variance, _ = jax.tree_util.tree_flatten(ctx.parameter_variance)  # same structure

    structure = str(structure)  # like "PyTreeDef({'2': {'a': *}})"
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

    for device in jax.local_devices():
        shard = device.id
        log(f"Uploading {shard=} to {ctx.training.checkpoint_path}/{shard}/", verbose)
        for tree, suffix in ((flattened, "parameters"), (variance, "variance")):
            write(index_weights(tree, shard), f"{ctx.training.checkpoint_path}/{shard}/{suffix}.npz")


def write_train_checkpoint(wctx: WhileTrainContext, verbose: bool = True):
    write_checkpoint(wctx.ctx, verbose)
    for device in jax.local_devices():
        shard = device.id
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
        unsharded.append(jnp.asarray(x))
    return unsharded


def _read_shards(path: str, structure, suffix: str):
    with multiprocessing.pool.ThreadPool(jax.local_device_count()) as p:
        start = time.time()
        paths = [f"{path}/{dev.id}/{suffix}.npz" for dev in jax.local_devices()]
        shards = list(p.map(read_shard, paths))
        print(f"Loading {suffix} took {time.time() - start:.2}s")

    return jax.tree_util.tree_unflatten(structure, unshard(shards))


def _overwrite(new: dict, old: dict, ignore: re.Pattern):
    print("Unknown:  ", [p for p in new.keys() if p not in old and not ignore.match(p)])
    print("Unfilled: ", [p for p in old.keys() if p not in new and not ignore.match(p)])

    if not old:
        for key, param in new.items():
            old[key] = param
        return

    for key in old.keys():
        if key in new:
            old[key] = new[key]


def read_checkpoint(ctx: Context, ignore: str = '.*optimizer.*', load_variance: bool = False):
    ignore = re.compile(ignore)

    with smart_open(f"{ctx.training.checkpoint_load_path}/structure.json", "r") as f:
        structure = f.read()
    structure = json.loads(structure)
    structure = deep_replace(structure, jnp.zeros((1,)))
    _, structure = jax.tree_util.tree_flatten(structure)

    _overwrite(_read_shards(ctx.training.checkpoint_load_path, structure, "parameters"), ctx.parameters, ignore)
    if load_variance:
        _overwrite(_read_shards(ctx.training.checkpoint_load_path, structure, "variance"), ctx.parameter_variance,
                   ignore)


def read_train_checkpoint(wctx: WhileTrainContext, ignore: str = '.*optimizer.*'):
    read_checkpoint(wctx.ctx, ignore, load_variance=True)

    _, structure = jax.tree_util.tree_flatten([jnp.zeros((1,))])
    wctx.loss = _read_shards(wctx.ctx.training.checkpoint_load_path, structure, "loss")
    wctx.accuracy = _read_shards(wctx.ctx.training.checkpoint_load_path, structure, "accuracy")
    wctx.current_step = _read_shards(wctx.ctx.training.checkpoint_load_path, structure, "current_step")
