"""Build local SWE-bench eval images for a subset, via the official swebench 5.0.2 builder.

The prebuilt images are not on any public registry for our instances, so we build them
exactly the way the official harness does: per-instance Dockerfiles from the
``swe-bench/swe-bench-dockerfiles`` task repo, image names taken from the official
dataset's ``image`` column (swebench 5.0.2 tags, e.g.
``swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest``).

Usage:
    # canary: one image
    uv run python -m bench.prepare_images --ids-file bench/subsets/verified-20.jsonl --only astropy__astropy-12907
    # everything in the subset
    uv run python -m bench.prepare_images --ids-file bench/subsets/verified-20.jsonl --workers 3
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("bench.prepare_images")

DEFAULT_TASK_REPO = "/tmp/opencode/sweb-dockerfiles"
TASK_REPO_GITHUB = "swe-bench/swe-bench-dockerfiles"


def load_ids(ids_file: Path, only: str | None) -> list[str]:
    ids = [
        json.loads(line)["instance_id"]
        for line in ids_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if only:
        if only not in ids:
            raise SystemExit(f"--only {only!r} is not in {ids_file}")
        ids = [only]
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids-file", default="bench/subsets/verified-20.jsonl")
    parser.add_argument(
        "--task-repo",
        default=DEFAULT_TASK_REPO,
        help=f"local clone of {TASK_REPO_GITHUB} (cloned automatically if missing)",
    )
    parser.add_argument("--only", default=None, help="build a single instance_id (canary)")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="rebuild even if image exists")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    repo = Path(args.task_repo)
    if not repo.is_dir():
        logger.info("cloning task repo %s -> %s", TASK_REPO_GITHUB, repo)
        import subprocess

        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", f"https://github.com/{TASK_REPO_GITHUB}.git", str(repo)],
            check=True,
        )

    import docker
    from swebench.harness.run_evaluation import make_test_spec
    from swebench.harness.utils import load_swebench_dataset
    from swebench.image_builder.docker_build import build_instance_images
    from swebench.image_builder.image_spec import get_image_specs_from_dataset
    from swebench.task.repo import load_dockerfiles, task_paths

    ids = load_ids(Path(args.ids_file), args.only)
    logger.info("preparing %d image(s): %s", len(ids), ", ".join(ids))

    dataset = load_swebench_dataset("SWE-bench/SWE-bench_Verified", "test", instance_ids=ids)
    dataset = list(dataset)
    wanted = {d["instance_id"]: make_test_spec(d).image for d in dataset}
    for iid, image in sorted(wanted.items()):
        logger.info("  %s -> %s", iid, image)

    client = docker.from_env()
    dockerfiles = load_dockerfiles(repo, list(wanted))
    contexts = task_paths(repo, list(wanted))
    sample = next(iter(wanted.values()))
    namespace = sample.split("/")[0] if "/" in sample else None
    tag = sample.rsplit(":", 1)[1] if ":" in sample.rsplit("/", 1)[-1] else "latest"
    specs = get_image_specs_from_dataset(dataset, dockerfiles, namespace, tag, contexts)
    if not specs:
        raise SystemExit("no dockerfiles matched — task repo layout mismatch?")

    t0 = time.perf_counter()
    build_instance_images(
        client=client, image_specs=specs, force_rebuild=args.force, max_workers=args.workers
    )
    logger.info("build loop finished in %.0f s", time.perf_counter() - t0)

    missing = []
    for iid, image in sorted(wanted.items()):
        try:
            client.images.get(image)
            logger.info("READY %s", image)
        except docker.errors.ImageNotFound:
            missing.append((iid, image))
            logger.error("MISSING %s", image)
    if missing:
        logger.error("%d image(s) failed to build", len(missing))
        return 1
    logger.info("all %d image(s) ready locally", len(wanted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
