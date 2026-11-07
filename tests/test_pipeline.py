from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dws_dm_agent.runners.runtime import AgentRuntime


def _config(pipeline: list[str] | None = None) -> dict:
    preset: dict = {
        "supplement_prompt": "prompts/supplement.md",
        "front": {"agent": "front-agent", "prompt": "prompts/front.md"},
        "worker": {"agent": "work-agent", "prompt": "prompts/worker.md"},
    }
    if pipeline is not None:
        preset["pipeline"] = pipeline
    return {
        "agents": {
            "front-agent": {
                "driver": "pi",
                "protocol": "pi-rpc",
                "model": "m",
                "read_only": True,
                "timeout_seconds": 0,
                "options": {"tools": None},
            },
            "work-agent": {
                "driver": "pi",
                "protocol": "pi-rpc",
                "model": "m",
                "read_only": False,
                "timeout_seconds": 60,
                "options": {},
            },
        },
        "workflows": {"active": "default", "presets": {"default": preset}},
    }


def _runtime(root: Path, config: dict) -> AgentRuntime:
    import shutil

    shutil.copytree(Path(__file__).resolve().parents[1] / "prompts", root / "prompts")
    path = root / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return AgentRuntime.from_config(config, path, root)


class PipelineResolutionTests(unittest.TestCase):
    def test_default_derivation_matches_legacy_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(Path(tmp), _config())
        self.assertEqual(
            runtime.pipeline, ("gate", "front", "worker", "deliver")
        )

    def test_declared_pipeline_order_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(
                Path(tmp), _config(["gate", "worker", "front", "deliver"])
            )
        self.assertEqual(
            runtime.pipeline, ("gate", "worker", "front", "deliver")
        )

    def test_missing_deliver_is_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(Path(tmp), _config(["gate", "worker"]))
        self.assertEqual(runtime.pipeline, ("gate", "worker", "deliver"))

    def test_empty_list_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(Path(tmp), _config([]))
        self.assertEqual(
            runtime.pipeline, ("gate", "front", "worker", "deliver")
        )

    def test_duplicate_steps_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "duplicate"):
                _runtime(Path(tmp), _config(["gate", "worker", "worker", "deliver"]))

    def test_unknown_step_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unknown step"):
                _runtime(Path(tmp), _config(["gate", "bogus", "deliver"]))

    def test_pipeline_without_executive_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "executive"):
                _runtime(
                    Path(tmp),
                    _config(["gate", "front", "deliver"]),
                )

    def test_pipeline_step_without_stage_slot_rejected(self) -> None:
        config = _config(["gate", "front", "worker", "deliver"])
        config["workflows"]["presets"]["default"].pop("front")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unknown step"):
                _runtime(Path(tmp), config)


if __name__ == "__main__":
    unittest.main()
