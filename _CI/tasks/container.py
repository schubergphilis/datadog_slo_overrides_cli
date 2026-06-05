"""Container task definitions for building and running the deps image."""

import hashlib
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

from _CI.info import read as read_info

from .configuration import IMAGE_NAME
from .github import publish_deps_image
from .shared import container_engine, execute, is_ci, logged


@task
@logged('container.build')
def build(context: Context) -> None:
    """Build the dependency cache container image locally."""
    engine = container_engine()
    base_image = read_info('info.base-image')
    execute(
        context,
        f'{engine} build --build-arg BASE_IMAGE={base_image} -f Dockerfile.deps -t {IMAGE_NAME}:latest .',
    )


@task
@logged('container.publish')
def publish(context: Context) -> None:
    """Build the deps image and publish to a container registry (CI) or keep it local.

    In CI: delegates to the host-specific submodule (``github``)
    to log in and push.

    Locally: builds and tags the image without pushing anywhere.

    Writes the full image reference to ``.deps-image`` for downstream steps.
    """
    tag = hashlib.sha256(Path('uv.lock').read_bytes()).hexdigest()[:16]
    if is_ci():
        image = publish_deps_image(context, tag)
    else:
        image = f'{IMAGE_NAME}:{tag}'
        engine = container_engine()
        result = context.run(f'{engine} image inspect {image}', hide=True, warn=True)
        if result and not result.failed:
            print(f'Image already exists: {image}')
        else:
            base_image = read_info('info.base-image')
            execute(context, f'{engine} build --build-arg BASE_IMAGE={base_image} -f Dockerfile.deps -t {image} .')
    Path('.deps-image').write_text(image, encoding='utf-8')


namespace = Collection('container')
namespace.add_task(cast(Task, publish), default=True)
namespace.add_task(cast(Task, build))
