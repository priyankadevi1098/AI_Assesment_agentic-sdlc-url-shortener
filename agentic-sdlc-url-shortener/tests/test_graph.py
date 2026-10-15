"""Tests for task-graph validation (graph.py)."""
import unittest

from sdlc_agent.graph import GraphValidationError, Task, TaskGraph


class GraphTests(unittest.TestCase):
    def test_valid_graph_builds(self):
        graph = TaskGraph([
            Task("analysis", "analysis"),
            Task("gate", "governance_gate", depends_on=("analysis",)),
            Task("implementation", "implementation", depends_on=("gate",)),
        ])
        self.assertEqual(set(graph.tasks().keys()), {"analysis", "gate", "implementation"})

    def test_cycle_and_gate_bypass_rejected(self):
        # A direct cycle is rejected.
        with self.assertRaises(GraphValidationError) as cycle_ctx:
            TaskGraph([
                Task("a", "analysis", depends_on=("b",)),
                Task("b", "decomposition", depends_on=("a",)),
            ])
        self.assertIn("cycle", str(cycle_ctx.exception))

        # An implementation task with no governance_gate upstream is rejected,
        # even though the graph itself is acyclic.
        with self.assertRaises(GraphValidationError) as gate_ctx:
            TaskGraph([
                Task("analysis", "analysis"),
                Task("implementation", "implementation", depends_on=("analysis",)),
            ])
        self.assertIn("governance_gate", str(gate_ctx.exception))

    def test_duplicate_task_id_rejected(self):
        with self.assertRaises(GraphValidationError):
            TaskGraph([Task("a", "analysis"), Task("a", "decomposition")])

    def test_unknown_stage_type_rejected(self):
        with self.assertRaises(GraphValidationError):
            TaskGraph([Task("a", "not-a-real-stage")])

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(GraphValidationError):
            TaskGraph([Task("a", "analysis", depends_on=("missing",))])

    def test_topological_order_honours_dependencies(self):
        graph = TaskGraph([
            Task("analysis", "analysis"),
            Task("gate", "governance_gate", depends_on=("analysis",)),
            Task("implementation", "implementation", depends_on=("gate",)),
        ])
        order = graph.topological_order()
        self.assertLess(order.index("analysis"), order.index("gate"))
        self.assertLess(order.index("gate"), order.index("implementation"))


if __name__ == "__main__":
    unittest.main()
