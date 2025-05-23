"""
Agent module for creating agents that can process inputs, use tools, and hand off to other agents.
"""

import os
import json
import openai
from rich.align import Align
from rich.panel import Panel
from rich import print as rprint
from .config import GenerationParams
from agilemind.context import Context
from agilemind.tool import execute_tool
from typing import List, Optional, Dict, Union
from agilemind.utils import retry, calculate_cost, clean_json_string


class Agent:
    """
    An agent that can perform tasks based on instructions and use tools or hand off to other agents.

    Agents can process inputs according to their instructions, use tools to perform
    actions, or hand off to other specialized agents when appropriate.
    """

    def __init__(
        self,
        name: str,
        description: str,
        instructions: str,
        tools: Optional[List[Dict[str, str]]] = None,
        handoffs: Optional[List["Agent"]] = None,
        next_agent: Optional["Agent"] = None,
        model: str = "gpt-4o-mini",
        save_path: Optional[str] = None,
        generation_params: Optional[Union[GenerationParams, Dict]] = None,
        llm_base_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        multi_turn: bool = False,
    ):
        """
        Initialize an Agent instance.

        Args:
            name: The name of the agent. Should be unique, lowercase, and without spaces.
            description: Brief description of what the agent does
            instructions: Instructions that define the agent's behavior.
            tools: Optional list of tools the agent can use.
            handoffs: Optional list of agents this agent can hand off to.
            next_agent: Optional next agent for forced handoff regardless of the agent's decision.
            model: OpenAI model to use for this agent
            save_path: Optional path to save agent's responses for documentation purposes
            generation_params: Optional parameters for the model generation
            llm_base_url: Optional base URL for the OpenAI API
            llm_api_key: Optional API key for the OpenAI API
            multi_turn: If True, agent can process multiple rounds; if False, agent processes only one round
        """
        self.name = name
        self.description = description
        self.instructions = instructions
        self.tools = tools or []
        self.handoffs = handoffs or []
        self.next_agent = next_agent
        self.model = model
        self.save_path = save_path
        self.rounds = []
        self.generation_params = generation_params
        self.multi_turn = multi_turn
        self.memory = []

        if isinstance(generation_params, dict):
            self.generation_params = GenerationParams(**generation_params)

        api_key = llm_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            rprint(
                Panel(
                    Align.center(
                        "No OpenAI API key found. "
                        "Please set OPENAI_API_KEY in your environment "
                        "or use a .env/config.yaml file. "
                        "Please refer to the README for more information."
                    ),
                    title="[bold red]Error[/bold red]",
                    border_style="red",
                )
            )
            exit(1)

        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=llm_base_url or os.getenv("OPENAI_API_BASE_URL"),
        )

    def __repr__(self) -> str:
        """Return string representation of the Agent."""
        return f"Agent(name='{self.name}')"

    def __str__(self):
        """Return string representation of the Agent."""
        return f"Agent(name='{self.name}')"

    def get_available_tools(self) -> List[Dict[str, str]]:
        """Return the list of tools available to this agent."""
        return self.tools

    def get_available_handoffs(self) -> List["Agent"]:
        """Return the list of agents this agent can hand off to."""
        return self.handoffs

    def set_model(self, model: str) -> None:
        """
        Set the model to use for this agent.

        Args:
            model (str): The model to use
        """
        self.model = model

    def save_response(self, response_content: str) -> None:
        """
        Save the agent's response to the specified path.

        Args:
            response_content: The content to save to the file
        """
        if not self.save_path:
            return

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(self.save_path), exist_ok=True)

        # Append to file if it exists, otherwise create it
        with open(self.save_path, "a") as f:
            f.write(response_content + "\n\n")

    def process(
        self,
        context: Context,
        input_text: str,
        max_iterations: int = None,
        clear_memory: bool = True,
    ) -> Dict:
        """
        Process the input using OpenAI API and return the agent's response.
        This method is decorated with retry to handle transient failures.

        Args:
            context: The context object
            input_text: The text input to process
            max_iterations: Maximum number of interaction rounds (None means no limit)
            clear_memory: If True, clears the memory before processing; if False, appends to existing memory

        Returns:
            Dict: containing:
                - input: Initial input text
                - output: Model response message of final round
                - usage: Dict with token usage and cost information
                - reason: Reason for ending the conversation
                - handoff: Dict with agent object and instructions if handoff occurred
        """
        result = self._process_with_retry(
            context, input_text, max_iterations, clear_memory
        )
        context.add_history(self.name, result.copy())
        return result

    @retry(
        exceptions=[
            openai.APIError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.InternalServerError,
            json.JSONDecodeError,
        ],
    )
    def _process_with_retry(
        self,
        context: Context,
        input_text: str,
        max_iterations: int = None,
        clear_memory: bool = True,
    ) -> Dict:
        """
        Internal method to process input with retry capabilities.
        This method is decorated with retry to handle transient failures.

        Args:
            context: The context object
            input_text: The text input to process
            max_iterations: Maximum number of interaction rounds (None means no limit)
            clear_memory: If True, clears the memory before processing; if False, appends to existing memory

        Returns:
            Dict: containing:
                - input: Initial input text
                - output: Model response message of final round
                - usage: Dict with token usage and cost information
                - reason: Reason for ending the conversation
                - handoff: Dict with agent object and instructions if handoff occurred
        """
        messages = self._prepare_messages(input_text, clear_memory)

        # Initialize rounds tracking for internal use
        self.rounds = []
        current_round = {
            "input": input_text,
            "output": None,
            "tool_calls": None,
            "handoff": None,
            "token_usage": None,
            "cost": None,
            "reason": None,
        }

        # Initialize tracking for the final result
        total_token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        total_cost = {"prompt_cost": 0, "completion_cost": 0, "total_cost": 0}
        final_output = None
        final_reason = None
        handoff_target = None
        handoff_instructions = None

        # Add handoff agents as tools
        tools_with_handoffs = self.tools.copy()
        for agent in self.handoffs:
            tools_with_handoffs.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"handoff_to_{agent.name}",
                        "description": f"Hand off to the {agent.name}, who is specialized in {agent.description}.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "instructions": {
                                    "type": "string",
                                    "description": "Instructions for that character to follow.",
                                }
                            },
                        },
                    },
                }
            )

        round_number = 0

        # Continue conversation until no more tool calls or a handoff is requested
        # or until max_iterations is reached (if specified)
        while max_iterations is None or round_number < max_iterations:
            round_number += 1

            try:
                print(f"Debug: {self.name} processing round {round_number}...")
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools_with_handoffs if tools_with_handoffs else None,
                    **(
                        self.generation_params.to_dict()
                        if self.generation_params
                        else {}
                    ),
                )
                print("Debug: Response received.")
            except Exception as e:
                print(f"Error: {e}")
                raise

            response_message = response.choices[0].message
            final_output = response_message.content

            # Update current round with output
            current_round["output"] = response_message.content

            # Record token usage with enhanced details
            if hasattr(response, "usage") and response.usage:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens

                # Update total token usage
                total_token_usage["prompt_tokens"] += prompt_tokens
                total_token_usage["completion_tokens"] += completion_tokens
                total_token_usage["total_tokens"] += response.usage.total_tokens

                # Record token usage for current round
                current_round["token_usage"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

                # Calculate cost using the model_pricing utility
                cost_info = calculate_cost(
                    model=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )

                # Update total cost
                total_cost["prompt_cost"] += cost_info["prompt_cost"]
                total_cost["completion_cost"] += cost_info["completion_cost"]
                total_cost["total_cost"] += cost_info["total_cost"]

                # Record cost information for current round
                current_round["cost"] = cost_info

                # Update token usage in context with detailed information
                context.update_token_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    agent_name=self.name,
                    round_number=round_number,
                    model=self.model,
                )

                # Update cost information in context
                context.update_cost(
                    prompt_cost=cost_info["prompt_cost"],
                    completion_cost=cost_info["completion_cost"],
                    agent_name=self.name,
                    round_number=round_number,
                    model=self.model,
                )

            # Save the response if a save path is specified
            if self.save_path and response_message.content:
                if self.save_path.endswith(".json"):
                    self.save_response(clean_json_string(response_message.content))
                else:
                    self.save_response(response_message.content)

            # Check for handoff or tool calls
            handoff_requested = False
            current_round_tool_calls = []
            work_done_called = False

            if hasattr(response_message, "tool_calls") and response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    tool_name = tool_call.function.name

                    # Check if this is a handoff
                    if tool_name.startswith("handoff_to_"):
                        target_agent_name = tool_name[len("handoff_to_") :]
                        args = json.loads(tool_call.function.arguments)
                        handoff_instructions = args.get("instructions", "")

                        for agent in self.handoffs:
                            if agent.name == target_agent_name:
                                current_round["handoff"] = agent.name
                                current_round["reason"] = "handoff"
                                handoff_target = agent
                                final_reason = "handoff"
                                handoff_requested = True
                                break
                        if handoff_requested:
                            break
                    elif tool_name == "work_done":
                        # Mark that work_done was called
                        work_done_called = True
                        current_round["reason"] = "work_done"
                        final_reason = "work_done"
                    else:
                        # Execute the tool
                        args = json.loads(tool_call.function.arguments)
                        tool_result = execute_tool(context, tool_name, args)
                        current_tool_call = {
                            "tool": tool_name,
                            "args": args,
                            "result": tool_result,
                        }
                        current_round_tool_calls.append(current_tool_call)

                        # Add the tool call and result to messages for the next turn
                        messages.append(
                            {
                                "role": "assistant",
                                "content": response_message.content,
                                "tool_calls": [
                                    {
                                        "id": tool_call.id,
                                        "type": "function",
                                        "function": {
                                            "name": tool_name,
                                            "arguments": tool_call.function.arguments,
                                        },
                                    }
                                ],
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps(tool_result),
                            }
                        )

                        # Update memory with the new messages
                        if messages[-2] not in self.memory:  # Avoid duplicates
                            self.memory.append(messages[-2])
                        if messages[-1] not in self.memory:  # Avoid duplicates
                            self.memory.append(messages[-1])

            # Update current round with tool calls if any
            if current_round_tool_calls:
                current_round["tool_calls"] = current_round_tool_calls

            # Add the current round to rounds list
            self.rounds.append(current_round.copy())

            # Check if max iterations will be reached in the next round
            if max_iterations is not None and round_number >= max_iterations:
                # If this was the last round due to max_iterations, set the reason
                if not current_round["reason"]:
                    current_round["reason"] = "round_limit"
                    final_reason = "round_limit"
                    # Update the last entry in rounds list
                    self.rounds[-1]["reason"] = "round_limit"

            # Break the loop if no tool calls or handoff is requested
            # or if we've reached the maximum number of iterations
            # or if multi_turn is False (meaning we only want one round)
            if (
                not current_round_tool_calls
                or handoff_requested
                or work_done_called
                or (max_iterations is not None and round_number >= max_iterations)
                or not self.multi_turn  # Break after one round if multi_turn is False
            ):
                # If we're breaking without a specific reason, mark it as completed
                if not current_round["reason"]:
                    current_round["reason"] = "completed"
                    final_reason = "completed"
                    # Update the last entry in rounds list
                    self.rounds[-1]["reason"] = "completed"
                break

            # Prepare for next round - tool results become the new input
            next_input = f"Tool results: {json.dumps(tool_result)}"
            current_round = {
                "input": next_input,
                "output": None,
                "tool_calls": None,
                "handoff": None,
                "token_usage": None,
                "cost": None,
                "reason": None,
            }

        # Check if there's a forced handoff via next_agent, which takes precedence over
        # any handoff the agent might have selected
        if self.next_agent:
            # Update the last round with the forced handoff
            self.rounds[-1]["handoff"] = self.next_agent.name
            handoff_target = self.next_agent
            if self.rounds[-1]["reason"] != "work_done":
                self.rounds[-1]["reason"] = "handoff"
                final_reason = "handoff"

        # Construct the result dictionary
        result = {
            "input": input_text,
            "output": final_output,
            "usage": {
                "token": total_token_usage,
                "cost": total_cost,
            },
            "reason": final_reason,
            "handoff": (
                {
                    "to": handoff_target or None,
                    "instruction": handoff_instructions or "",
                }
                if handoff_target
                else None
            ),
        }

        return result

    def _prepare_messages(
        self, input_text: str, clear_memory: bool = True
    ) -> List[Dict]:
        """
        Prepare the message for the API call.

        Args:
            input_text: The text input to process
            clear_memory: If True, clears the memory before preparing messages;
                          if False, includes memory in the conversation
        """
        # Clear memory if requested
        if clear_memory:
            self.memory = []

        # Base messages with system instructions
        messages = [{"role": "system", "content": self.instructions}]

        # Add memory messages if they exist
        if self.memory:
            messages.extend(self.memory)

        # Add the new user message
        user_message = {"role": "user", "content": input_text}
        messages.append(user_message)

        # Store this message in memory if not clearing
        if not clear_memory:
            if user_message not in self.memory:
                self.memory.append(user_message)

        return messages
