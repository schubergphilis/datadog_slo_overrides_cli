"""CI task definitions for the project workflow."""

from invoke import Collection

from . import bootstrap, build, container, develop, document, format_, lint, quality, release, secure, test

namespace = Collection()
namespace.add_collection(bootstrap.namespace)
namespace.add_collection(build.namespace)
namespace.add_collection(container.namespace)
namespace.add_collection(develop.namespace)
namespace.add_collection(document.namespace)
namespace.add_collection(format_.namespace)
namespace.add_collection(lint.namespace)
namespace.add_collection(quality.namespace)
namespace.add_collection(release.namespace)
namespace.add_collection(secure.namespace)
namespace.add_collection(test.namespace)

# Wire bootstrap as a pre-task on all other top-level default tasks
bootstrap_task = bootstrap.bootstrap
for module in (build, container, develop, document, format_, lint, quality, release, secure, test):
    for task in module.namespace.tasks.values():
        task.pre.insert(0, bootstrap_task)
