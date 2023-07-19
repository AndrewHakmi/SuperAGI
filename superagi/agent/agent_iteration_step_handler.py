from datetime import datetime

from sqlalchemy import asc
from sqlalchemy.sql.operators import and_

import superagi
from superagi.agent.agent_message_builder import AgentLlmMessageBuilder
from superagi.agent.agent_prompt_builder import AgentPromptBuilder
from superagi.agent.output_handler import ToolOutputHandler, get_output_handler
from superagi.agent.task_queue import TaskQueue
from superagi.agent.tool_builder import ToolBuilder
from superagi.apm.event_handler import EventHandler
from superagi.config.config import get_config
from superagi.helper.token_counter import TokenCounter
from superagi.lib.logger import logger
from superagi.models.agent import Agent
from superagi.models.agent_config import AgentConfiguration
from superagi.models.agent_execution import AgentExecution
from superagi.models.agent_execution_config import AgentExecutionConfiguration
from superagi.models.agent_execution_feed import AgentExecutionFeed
from superagi.models.agent_execution_permission import AgentExecutionPermission
from superagi.models.organisation import Organisation
from superagi.models.tool import Tool
from superagi.models.workflows.agent_workflow_step import AgentWorkflowStep
from superagi.models.workflows.iteration_workflow import IterationWorkflow
from superagi.models.workflows.iteration_workflow_step import IterationWorkflowStep
from superagi.resource_manager.resource_summary import ResourceSummarizer
from superagi.tools.resource.query_resource import QueryResourceTool
from superagi.tools.thinking.tools import ThinkingTool


class AgentIterationStepHandler:
    def __init__(self, session, llm, agent_id: int, agent_execution_id: int, memory = None):
        self.session = session
        self.llm = llm
        self.agent_execution_id = agent_execution_id
        self.agent_id = agent_id
        self.memory = memory

    def execute_step(self, agent_workflow_step_id: int, iteration_workflow_step_id: int):
        agent_config = Agent.fetch_configuration(self.session, self.agent_id)
        execution = AgentExecution.get_agent_execution_from_id(self.session, self.agent_execution_id)
        iteration_workflow_step = IterationWorkflowStep.find_by_id(self.session, iteration_workflow_step_id)

        if not self.handle_wait_for_permission(execution, agent_config, iteration_workflow_step):
            return

        agent_execution_config = AgentExecutionConfiguration.fetch_configuration(self.session, self.agent_execution_id)
        workflow_step = AgentWorkflowStep.find_by_id(self.session, agent_workflow_step_id)
        organisation = Organisation.find_org_by_agent_id(self.session, agent_id=self.agent_id)
        iteration_workflow = IterationWorkflow.find_by_id(self.session, workflow_step.action_reference_id)

        task_queue = TaskQueue(str(self.agent_execution_id))

        agent_feeds = self.fetch_agent_feeds(self.agent_execution_id)
        if not agent_feeds:
            task_queue.clear_tasks()

        prompt = self.build_agent_prompt(iteration_workflow=iteration_workflow,
                                         agent_config=agent_config,
                                         agent_execution_config=agent_execution_config,
                                         prompt=workflow_step.prompt,
                                         task_queue=task_queue)

        messages = AgentLlmMessageBuilder(self.session, self.llm.get_model()) \
            .build_agent_messages(prompt, agent_feeds, history_enabled=workflow_step.history_enabled,
                                  completion_prompt=workflow_step.completion_prompt)

        logger.debug("Prompt messages:", messages)
        current_tokens = TokenCounter.count_message_tokens(messages, self.llm.get_model())
        response = self.llm.chat_completion(messages, TokenCounter.token_limit(self.llm.get_model()) - current_tokens)


        if 'content' not in response or response['content'] is None:
            raise RuntimeError(f"Failed to get response from llm")

        total_tokens = current_tokens + TokenCounter.count_message_tokens(response['content'], self.llm.get_model())
        AgentExecution.update_tokens(self.session, self.agent_execution_id, total_tokens)

        assistant_reply = response['content']
        output_handler = get_output_handler(self.agent_execution_id, workflow_step.output_type, agent_config)
        response = output_handler.handle(self.session, assistant_reply)

        response.status = "PENDING"

        if response.status == "COMPLETE":
            execution.status = "COMPLETED"
            self.session.commit()

            self.update_agent_execution_next_step(execution, iteration_workflow_step.next_step_id,
                                                  workflow_step, "COMPLETE")
            EventHandler(session=self.session).create_event('run_completed',
                                                            {'agent_execution_id': execution.id,
                                                             'name': execution.name,
                                                             'tokens_consumed': execution.num_of_tokens,
                                                             "calls": execution.num_of_calls},
                                                            execution.agent_id, organisation.id)
        elif response.status == "WAITING_FOR_PERMISSION":
            execution.status = "WAITING_FOR_PERMISSION"
            execution.permission_id = response.permission_id
            self.session.commit()
        else:
            # moving to next step of iteration or workflow
            self.update_agent_execution_next_step(execution, iteration_workflow_step.next_step_id, workflow_step)
            logger.info(f"Starting next job for agent execution id: {self.agent_execution_id}")
            superagi.worker.execute_agent.delay(self.agent_execution_id, datetime.now())

        self.session.flush()

    def update_agent_execution_next_step(self, execution, next_step_id, workflow_step, step_response: str = "default"):
        execution.iteration_workflow_step_id = next_step_id
        if execution.iteration_workflow_step_id == -1:
            next_step = AgentWorkflowStep.fetch_next_step(self.session, workflow_step, step_response)
            execution.current_step_id = next_step.id
        self.session.commit()

    def build_agent_prompt(self, iteration_workflow: IterationWorkflow, agent_config: dict,
                           agent_execution_config: dict,
                           prompt: str, task_queue: TaskQueue):
        max_token_limit = int(get_config("MAX_TOOL_TOKEN_LIMIT", 600))
        agent_tools = self.build_tools(agent_config, agent_execution_config)
        prompt = AgentPromptBuilder.replace_main_variables(prompt, agent_execution_config["goal"],
                                                           agent_execution_config["instruction"],
                                                           agent_config["constraints"], agent_tools,
                                                           (not iteration_workflow.has_task_queue))

        if iteration_workflow.has_task_queue:
            response = task_queue.get_last_task_details()
            last_task, last_task_result = (response["task"], response["response"]) if response is not None else ("", "")
            current_task = task_queue.get_first_task() or ""
            token_limit = TokenCounter.token_limit() - max_token_limit
            prompt = AgentPromptBuilder.replace_task_based_variables(prompt, current_task, last_task, last_task_result,
                                                                     task_queue.get_tasks(),
                                                                     task_queue.get_completed_tasks(), token_limit)
        return prompt


    def build_tools(self, agent_config: dict, agent_execution_config: dict):
        agent_tools = [ThinkingTool()]

        model_api_key = AgentConfiguration.get_model_api_key(self.session, self.agent_id, agent_config["model"])
        tool_builder = ToolBuilder(self.session, self.agent_id, self.agent_execution_id)
        resource_summary = ResourceSummarizer(session=self.session,
                                              agent_id=self.agent_id).fetch_or_create_agent_resource_summary(
            default_summary=agent_config.get("resource_summary"))
        if resource_summary is not None:
            agent_tools.append(QueryResourceTool())
        user_tools = self.session.query(Tool).filter(
            and_(Tool.id.in_(agent_config["tools"]), Tool.file_name is not None)).all()
        for tool in user_tools:
            agent_tools.append(tool_builder.build_tool(tool))

        agent_tools = [tool_builder.set_default_params_tool(tool, agent_config, agent_execution_config,
                                                            model_api_key, resource_summary) for tool in agent_tools]
        return agent_tools


    def fetch_agent_feeds(self, agent_execution_id):
        agent_feeds = self.session.query(AgentExecutionFeed.role, AgentExecutionFeed.feed) \
            .filter(AgentExecutionFeed.agent_execution_id == agent_execution_id) \
            .order_by(asc(AgentExecutionFeed.created_at)) \
            .all()
        return agent_feeds[2:]

    def handle_wait_for_permission(self, agent_execution, agent_config: dict,
                                   iteration_workflow_step: IterationWorkflowStep):
        """
        Handles the wait for permission when the agent execution is waiting for permission.

        Args:
            agent_execution (AgentExecution): The agent execution.
            spawned_agent (SuperAgi): The spawned agent.
            session (Session): The database session object.

        Raises:
            ValueError: If the permission is still pending.
        """
        if agent_execution.status != "WAITING_FOR_PERMISSION":
            return True
        agent_execution_permission = self.session.query(AgentExecutionPermission).filter(
            AgentExecutionPermission.id == agent_execution.permission_id).first()
        if agent_execution_permission.status == "PENDING":
            logger.error("handle_wait_for_permission: Permission is still pending")
            return False
        if agent_execution_permission.status == "APPROVED":
            tool_output_handler = ToolOutputHandler(self.agent_execution_id, agent_config)
            tool_result = tool_output_handler.handle_tool_response(self.session, agent_execution_permission.assistant_reply).get("result")
            result = tool_result.result
        else:
            result = f"User denied the permission to run the tool {agent_execution_permission.tool_name}" \
                     f"{' and has given the following feedback : ' + agent_execution_permission.user_feedback if agent_execution_permission.user_feedback else ''}"

        agent_execution_feed = AgentExecutionFeed(agent_execution_id=agent_execution_permission.agent_execution_id,
                                                  agent_id=agent_execution_permission.agent_id,
                                                  feed=result, role="user")
        self.session.add(agent_execution_feed)
        agent_execution.status = "RUNNING"
        agent_execution.current_step_id = iteration_workflow_step.next_step_id
        self.session.commit()
        return True

