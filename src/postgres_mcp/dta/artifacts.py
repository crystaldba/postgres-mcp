"""Artifacts for the Database Tuning Advisor."""

from difflib import unified_diff


class ExplainPlanArtifact:
    """Formats explain plans as human-readable text."""

    @staticmethod
    def format_plan_summary(plan_data):
        """Format the plan summary as text."""
        # Return empty string if no plan data
        if not plan_data:
            return ""

        # Initialize the output as empty list
        output = []

        # Get the plan
        plan = plan_data.get("Plan", {})

        # Format the plan
        ExplainPlanArtifact._format_plan_node(plan, output, 0)

        # Join the output with newlines and return
        return "\n".join(output)

    @staticmethod
    def _format_plan_node(node, output, depth):
        """
        Format a single node in the plan.

        Args:
            node: The node to format
            output: The output list to append to
            depth: The depth of the node in the tree
        """
        # Add indentation
        indent = "  " * depth

        # Get node type
        node_type = node.get("Node Type", "Unknown")

        # Get cost
        startup_cost = node.get("Startup Cost", 0)
        total_cost = node.get("Total Cost", 0)

        # Get other important fields
        rows = node.get("Plan Rows", 0)
        width = node.get("Plan Width", 0)

        # Format the node
        node_line = f"{indent}-> {node_type}"

        # Add cost
        cost_str = (
            f"(cost={startup_cost:.2f}..{total_cost:.2f} rows={rows} width={width})"
        )
        node_line += f" {cost_str}"

        # Add important node-specific details
        if node_type == "Seq Scan":
            relation = node.get("Relation Name", "")
            node_line += f" on {relation}"
            filter_cond = node.get("Filter", "")
            if filter_cond:
                filter_line = f"{indent}   Filter: {filter_cond}"
                output.append(node_line)
                output.append(filter_line)
                return
        elif node_type == "Index Scan" or node_type == "Index Only Scan":
            relation = node.get("Relation Name", "")
            index_name = node.get("Index Name", "")
            node_line += f" on {relation} using {index_name}"
            filter_cond = node.get("Filter", "")
            if filter_cond:
                filter_line = f"{indent}   Filter: {filter_cond}"
                output.append(node_line)
                output.append(filter_line)
                return

        # Add the formatted node to output
        output.append(node_line)

        # Process child nodes if any
        if "Plans" in node:
            for child in node["Plans"]:
                ExplainPlanArtifact._format_plan_node(child, output, depth + 1)

    @staticmethod
    def create_plan_diff(before_plan, after_plan):
        """Create a diff between before and after explain plans."""
        # Format both plans as text
        before_text = ExplainPlanArtifact.format_plan_summary(before_plan).split("\n")
        after_text = ExplainPlanArtifact.format_plan_summary(after_plan).split("\n")

        # Generate a unified diff
        diff_lines = unified_diff(
            before_text, after_text, fromfile="before", tofile="after", lineterm=""
        )

        # Join the diff lines
        return "\n".join(diff_lines)


def calculate_improvement_multiple(base_cost, new_cost):
    """Calculate the improvement multiple as base_cost / new_cost."""
    if new_cost is None or new_cost <= 0:
        return float("inf")
    if base_cost is None or base_cost <= 0:
        return 1.0

    return base_cost / new_cost
