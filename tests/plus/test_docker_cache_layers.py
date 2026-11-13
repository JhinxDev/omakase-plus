"""Dependency install inputs stay separate from application wheel/source inputs."""

from pathlib import Path


def test_dependency_layers_precede_source_and_wheel_and_remain_hash_locked():
    docker = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    logical = " ".join(line.rstrip(chr(92)) for line in docker.splitlines())
    builder, runtime = logical.split(" AS runtime", 1)
    builder = builder.split(" AS builder", 1)[1]
    copies = builder[: builder.index("RUN pip install")]
    assert "COPY requirements-build.txt ./" in copies
    assert "COPY src" not in copies and "COPY pyproject.toml" not in copies
    assert (
        builder.index("--require-hashes -r requirements-build.txt")
        < builder.index("COPY src")
        < builder.index("RUN python -m build")
    )
    assert (
        runtime.index("--require-hashes -r /tmp/requirements-$requirements.txt")
        < runtime.index("COPY --from=builder /wheels")
        < runtime.index("RUN pip install --no-cache-dir --no-deps /tmp/*.whl")
    )
    assert "ARG OMAKASE_AGENT_TOOLS=true" in runtime and "USER omakase" in runtime
    assert (
        logical.count(
            "python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f"
        )
        == 2
    )
