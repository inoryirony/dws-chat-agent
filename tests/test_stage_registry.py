from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dws_dm_agent import stages
from dws_dm_agent.runners.runtime import AgentRuntime
from dws_dm_agent.skills_catalog import resolve_skills, skills_prompt_block


def _base_config() -> dict:
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
        "workflows": {
            "active": "default",
            "presets": {
                "default": {
                    "supplement_prompt": "prompts/supplement.md",
                    "front": {"agent": "front-agent", "prompt": "prompts/front.md"},
                    "worker": {"agent": "work-agent", "prompt": "prompts/worker.md"},
                }
            },
        },
        "skills": {"demo": "/nonexistent/skills/demo/SKILL.md"},
    }


def _write_config(root: Path, config: dict) -> Path:
    import shutil

    shutil.copytree(Path(__file__).resolve().parents[1] / "prompts", root / "prompts")
    path = root / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


class StageRegistryTests(unittest.TestCase):
    def test_conventional_slots_map_to_kinds(self) -> None:
        self.assertEqual(stages.stage_spec("front").kind, "triage")
        self.assertEqual(stages.stage_spec("worker").kind, "executive")
        self.assertTrue(stages.stage_spec("worker").executive)
        self.assertFalse(stages.stage_spec("front").executive)

    def test_unknown_slot_defaults_to_executive(self) -> None:
        spec = stages.stage_spec("verifier")
        self.assertTrue(spec.executive)
        self.assertFalse(spec.mount_escalate)

    def test_register_stage_overrides_slot_without_touching_kind(self) -> None:
        try:
            stages.register_stage("verifier", "triage", use_worktree=True)
            spec = stages.stage_spec("verifier")
            self.assertEqual(spec.kind, "triage")
            self.assertTrue(spec.use_worktree)
            self.assertFalse(stages.stage_spec("front").use_worktree)
        finally:
            stages._SLOT_KINDS.pop("verifier", None)
            stages._SLOT_OVERRIDES.pop("verifier", None)

    def test_runtime_stage_of_kind_resolves_custom_slots(self) -> None:
        config = _base_config()
        # 未注册的自定义槽位默认 executive，需要 read_only=false 的 agent。
        config["workflows"]["presets"]["default"]["verifier"] = {
            "agent": "work-agent",
            "prompt": "prompts/worker.md",
        }
        stages.register_stage("screener", "triage")
        try:
            config["workflows"]["presets"]["default"]["screener"] = {
                "agent": "front-agent",
                "prompt": "prompts/front.md",
            }
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(Path(tmp), config)
                runtime = AgentRuntime.from_config(config, path, Path(tmp))
                self.assertEqual(runtime.stage_of_kind("triage"), "front")
                self.assertEqual(runtime.stage_of_kind("executive"), "worker")
                self.assertIn("screener", runtime.pipeline)
                self.assertIn("verifier", runtime.pipeline)
        finally:
            stages._SLOT_KINDS.pop("screener", None)
            stages._SLOT_OVERRIDES.pop("screener", None)

    def test_runtime_requires_executive_stage(self) -> None:
        config = _base_config()
        preset = config["workflows"]["presets"]["default"]
        preset.pop("worker")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp), config)
            with self.assertRaisesRegex(ValueError, "executive"):
                AgentRuntime.from_config(config, path, Path(tmp))


def _make_skill(root: Path, name: str, description: str) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文\n",
        encoding="utf-8",
    )
    return path


class SkillsProvisionTests(unittest.TestCase):
    def _runtime(self, root: Path, options: dict) -> AgentRuntime:
        config = _base_config()
        config["agents"]["work-agent"]["options"] = options
        path = _write_config(root, config)
        return AgentRuntime.from_config(config, path, root)

    def test_resolve_skills_fail_loud_on_unknown_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in config skills pool"):
            resolve_skills({"a": "/tmp/a/SKILL.md"}, ["missing"])

    def test_resolve_skills_fail_loud_on_broken_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not resolve"):
            resolve_skills({"a": "/nonexistent/a/SKILL.md"}, ["a"])

    def test_prompt_block_lists_name_description_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _make_skill(root, "demo", "把正文改写成人类口吻")
            block = skills_prompt_block([("demo", path)])
        self.assertIn("demo", block)
        self.assertIn("把正文改写成人类口吻", block)
        self.assertIn(str(path), block)

    def test_pi_prepare_passes_skill_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _make_skill(root, "demo", "d")
            runtime = self._runtime(root, {"skills": ["demo"]})
            runtime._skill_pool["demo"] = str(skill)
            prepared = runtime.prepare(
                "worker", "sess-1", "hi", root / "sess"
            )
        joined = " ".join(prepared.argv)
        self.assertIn("--skill", joined)
        self.assertIn("demo", joined)

    def test_codex_prepare_stages_skill_symlink_in_session_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _make_skill(root, "demo", "d")
            config = _base_config()
            config["agents"]["work-agent"] = {
                "driver": "codex",
                "protocol": "codex-app-server",
                "command": ["codex"],
                "model": "m",
                "read_only": False,
                "timeout_seconds": 60,
                "options": {"skills": ["demo"], "service_tools_mcp": True},
            }
            path = _write_config(root, config)
            runtime = AgentRuntime.from_config(config, path, root)
            runtime._skill_pool["demo"] = str(skill)
            runtime.prepare("worker", "sess-1", "hi", root / "sess")
            staged = root / "sess" / "codex-home" / "skills" / "demo"
            self.assertTrue(staged.is_symlink())

    def test_claude_prepare_builds_plugin_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _make_skill(root, "demo", "d")
            config = _base_config()
            config["agents"]["work-agent"] = {
                "driver": "claude",
                "protocol": "claude-stream-json",
                "command": ["claude"],
                "model": "m",
                "read_only": False,
                "timeout_seconds": 60,
                "options": {"skills": ["demo"], "service_tools_mcp": True},
            }
            path = _write_config(root, config)
            runtime = AgentRuntime.from_config(config, path, root)
            runtime._skill_pool["demo"] = str(skill)
            prepared = runtime.prepare("worker", "sess-1", "hi", root / "sess")
            manifest = root / "sess" / "claude-plugin" / ".claude-plugin" / "plugin.json"
            staged = root / "sess" / "claude-plugin" / "skills" / "demo"
            self.assertTrue(manifest.is_file())
            self.assertTrue(staged.is_symlink())
            self.assertIn("--plugin-dir", " ".join(prepared.argv))

    def test_dsh_prepare_generates_session_patch_with_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _make_skill(root, "demo", "d")
            config = _base_config()
            config["agents"]["work-agent"] = {
                "driver": "dsh",
                "protocol": "dsh-headless-jsonl",
                "command": ["dsh"],
                "model": "m",
                "read_only": False,
                "timeout_seconds": 60,
                "options": {
                    "skills": ["demo"],
                    "service_tools_mcp": True,
                    "dsh_profile": "headless",
                },
            }
            path = _write_config(root, config)
            runtime = AgentRuntime.from_config(config, path, root)
            runtime._skill_pool["demo"] = str(skill)
            runtime.prepare("worker", "sess-1", "hi", root / "sess")
            patch = root / "sess" / "dws-session.patch.yml"
            text = patch.read_text(encoding="utf-8")
            self.assertIn("skill-filesystem", text)
        self.assertIn("customSkillDirs", text)
        self.assertIn("includeDefaultRoots: false", text)
        self.assertIn("mcp-dws-tools", text)

    def test_prompt_variables_expose_skills_block(self) -> None:
        from dws_dm_agent.service import AgentService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _make_skill(root, "demo", "把正文改写成人类口吻")
            config = _base_config()
            config["agents"]["work-agent"]["options"] = {"skills": ["demo"]}
            path = _write_config(root, config)
            runtime = AgentRuntime.from_config(config, path, root)
            runtime._skill_pool["demo"] = str(skill)
            service = AgentService.__new__(AgentService)
            service.agent_runtime = runtime
            from types import SimpleNamespace

            service.settings = SimpleNamespace(
                timezone=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"),
                self_name="Op",
                workspace_root=root,
                worktree_root=root,
                state_dir=root / "state",
                raw=config,
            )
            from dws_dm_agent.core import AuditStore

            service.store = AuditStore(root / "audit.sqlite3")
            from dws_dm_agent.core import IncomingEvent
            from datetime import datetime

            batch = [
                IncomingEvent(
                    "e1",
                    "m1",
                    "c1",
                    SimpleNamespace(
                        display_name="C",
                        alias="c",
                        user_id="u",
                        open_dingtalk_id="o",
                        identity="o",
                    ),
                    "hi",
                    datetime(2026, 11, 7, 10, 0, tzinfo=service.settings.timezone),
                )
            ]
            variables = service._prompt_variables("worker", "sess-1", batch, "", ())
            service.store.close()
        self.assertIn("把正文改写成人类口吻", variables["skills"])


if __name__ == "__main__":
    unittest.main()


class AdapterKindDispatchTests(unittest.TestCase):
    """adapter.mcp_config 按 StageSpec.kind 分发（不比 stage 名）。

    自定义 triage/executive 槽位与 front/worker 同权：triage 挂 escalate，
    executive 挂 options.mcp + dws-tools。旧实现按 stage 名字符串比较，
    自定义槽位会静默丢 MCP——这是回归锁。
    """

    def _codex_runtime(self, root: Path) -> "AgentRuntime":
        config = _base_config()
        config["agents"]["front-agent"]["driver"] = "codex"
        config["agents"]["front-agent"]["protocol"] = "codex-app-server"
        config["agents"]["front-agent"]["options"] = {"service_tools_mcp": True}
        config["agents"]["work-agent"]["driver"] = "codex"
        config["agents"]["work-agent"]["protocol"] = "codex-app-server"
        config["agents"]["work-agent"]["options"] = {
            "mcp": {"grafana-dev": True},
            "service_tools_mcp": True,
        }
        path = _write_config(root, config)
        return AgentRuntime.from_config(config, path, root)

    def test_custom_triage_slot_mounts_escalate_like_front(self) -> None:
        stages.register_stage("screener", "triage")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._codex_runtime(root)
                config = json.loads(
                    (root / "config.json").read_text(encoding="utf-8")
                )
                config["workflows"]["presets"]["default"]["screener"] = {
                    "agent": "front-agent",
                    "prompt": "prompts/front.md",
                }
                (root / "config.json").write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                runtime = AgentRuntime.from_config(config, root / "config.json", root)
                prepared = runtime.prepare(
                    "screener", "front-agent", "prompt", root / "screener-run"
                )
                mcp = prepared.result_path.parent / "mcp.json"
                self.assertTrue(mcp.is_file())
                self.assertIn("dws-escalate", mcp.read_text(encoding="utf-8"))
        finally:
            stages._SLOT_KINDS.pop("screener", None)
            stages._SLOT_OVERRIDES.pop("screener", None)

    def test_custom_executive_slot_mounts_mcp_like_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._codex_runtime(root)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            config["workflows"]["presets"]["default"]["executor"] = {
                "agent": "work-agent",
                "prompt": "prompts/worker.md",
            }
            (root / "config.json").write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8"
            )
            runtime = AgentRuntime.from_config(config, root / "config.json", root)
            prepared = runtime.prepare(
                "executor", "work-agent", "prompt", root / "executor-run"
            )
            mcp = prepared.result_path.parent / "mcp.json"
            self.assertTrue(mcp.is_file())
            text = mcp.read_text(encoding="utf-8")
            self.assertIn("grafana-dev", text)
            self.assertIn("dws-tools", text)
