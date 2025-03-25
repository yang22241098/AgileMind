import os
import json
from typing import Dict
from pathlib import Path
from tool import get_all_tools
from prompt import waterfall_prompt
from execution import Agent, Runner

quality_assurance = Agent(
    "quality_assurance",
    "assure software quality",
    waterfall_prompt.QUALITY_ASSURANCE_PROMPT,
)
programmer = Agent(
    "programmer",
    "implement software",
    waterfall_prompt.PROGRAMER_PROMPT,
    tools=get_all_tools(),
    handoffs=[quality_assurance],
)
quality_assurance.next_agent = programmer
architect = Agent(
    "architect",
    "create software architecture",
    waterfall_prompt.ARCHITECT_PROMPT,
    next_agent=programmer,  # Force handoff to programmer
)
demand_analyst = Agent(
    "demand_analyst",
    "analyze user demand",
    waterfall_prompt.DEMAND_ANALYST_PROMPT,
    next_agent=architect,  # Force handoff to architect
)


def dev(demand: str, output: str, model: str) -> Dict[str, str]:
    """
    Run the LLM-Agent workflow pipelines.

    Args:
        demand: User demand for the software
        output: Directory path to save the software
        model: String name of the model to use

    Returns:
        Dictionary containing the software development process
    """
    Path(output).mkdir(parents=True, exist_ok=True)

    # Change current working directory to the output directory
    initial_cwd = os.getcwd()
    os.chdir(output)

    try:
        runner = Runner()
        result = runner.run(demand_analyst, demand, 5)

        with open("trace.txt", "w") as f:
            f.write(json.dumps(result, indent=4))
    finally:
        os.chdir(initial_cwd)  # Restore original working directory

    return result
