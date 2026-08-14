#!/usr/bin/env python3
"""Behavioral contracts for schema-bound audited workflows."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = REPOSITORY_ROOT / "controller" / "plugin" / "audited-workflows"
AGENT_ROOT = REPOSITORY_ROOT / "controller" / "plugin" / "agents"
ROUTER_SKILL = (
    REPOSITORY_ROOT
    / "controller"
    / "plugin"
    / "skills"
    / "heavy-orchestration"
    / "SKILL.md"
)


class AuditedWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is required to exercise workflow JavaScript")

    def run_workflow(
        self,
        workflow: str,
        *,
        failed_labels: list[str],
        high_risk: bool = False,
    ) -> dict:
        source_path = WORKFLOW_ROOT / f"{workflow}.js"
        input_name = "subject" if workflow == "review" else "question"
        harness = r"""
const fs = require('fs')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^export const meta/m, 'const meta')
const failedLabels = new Set(JSON.parse(process.argv[2]))
const highRisk = process.argv[3] === 'true'
const inputName = process.argv[4]
const workflow = new AsyncFunction('args', 'parallel', 'agent', 'log', source)
const parallel = tasks => Promise.all(tasks.map(task => task()))
const agent = async (_prompt, options) => {
  if (failedLabels.has(options.label)) {
    throw new Error('agent({schema}): subagent completed without calling StructuredOutput')
  }
  if (options.label === 'high-risk-adjudication') {
    return { decision: 'accept', blockingRisks: [], failureModes: [], validation: [] }
  }
  if (inputName === 'subject') {
    return { verdict: 'accept', findings: [], gaps: [] }
  }
  return { conclusion: 'confirmed', evidence: [], uncertainty: [] }
}
const args = { scope: 'repository evidence only', highRisk }
args[inputName] = 'bounded test'
workflow(args, parallel, agent, () => {}).then(
  result => process.stdout.write(JSON.stringify(result)),
  error => {
    process.stderr.write(String(error && error.stack ? error.stack : error))
    process.exitCode = 1
  },
)
"""
        completed = subprocess.run(
            [
                self.node,
                "-e",
                harness,
                str(source_path),
                json.dumps(failed_labels),
                str(high_risk).lower(),
                input_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_review_preserves_successful_sibling_when_verifier_fails(self) -> None:
        result = self.run_workflow("review", failed_labels=["verification"])

        self.assertEqual(result["status"], "degraded")
        self.assertIsNone(result["verification"])
        self.assertEqual(result["critique"]["verdict"], "accept")
        self.assertEqual(result["missingAgents"][0]["label"], "verification")
        self.assertIn("StructuredOutput", result["missingAgents"][0]["reason"])

    def test_investigate_reports_failed_when_all_evidence_agents_fail(self) -> None:
        result = self.run_workflow(
            "investigate",
            failed_labels=["evidence-map", "falsification"],
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["evidence"], [None, None])
        self.assertEqual(
            [missing["label"] for missing in result["missingAgents"]],
            ["evidence-map", "falsification"],
        )

    def test_adjudicator_failure_degrades_completed_reviews(self) -> None:
        result = self.run_workflow(
            "review",
            failed_labels=["high-risk-adjudication"],
            high_risk=True,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["verification"]["verdict"], "accept")
        self.assertEqual(result["critique"]["verdict"], "accept")
        self.assertIsNone(result["adjudication"])
        self.assertEqual(result["missingAgents"][0]["label"], "high-risk-adjudication")

    def test_schema_bound_agents_budget_structured_finalization(self) -> None:
        for agent_name in (
            "repository-verifier",
            "repository-explorer",
            "correctness-critic",
            "architecture-advisor",
        ):
            with self.subTest(agent=agent_name):
                definition = (AGENT_ROOT / f"{agent_name}.md").read_text(encoding="utf-8")
                self.assertIn("StructuredOutput", definition)
                self.assertIn("inspection rounds", definition)
                self.assertIn("final action", definition)

    def test_controller_collects_live_evidence_before_read_only_workflow(self) -> None:
        router = " ".join(ROUTER_SKILL.read_text(encoding="utf-8").split())

        self.assertIn("collect live cloud or remote-service evidence", router)
        self.assertIn("pass a bounded summary", router)
        self.assertIn("Do not ask repository agents to collect live evidence", router)

    def test_router_uses_adaptive_routing_without_numeric_thresholds(self) -> None:
        router = " ".join(ROUTER_SKILL.read_text(encoding="utf-8").split())

        self.assertIn("controller's current evidence", router)
        self.assertNotIn("at least two independent investigations", router)
        self.assertNotIn("at least eight items", router)

if __name__ == "__main__":
    unittest.main()
