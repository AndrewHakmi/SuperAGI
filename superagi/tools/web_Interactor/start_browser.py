from superagi.tools.base_tool import BaseTool
from pydantic import BaseModel, Field
from superagi.helper.browser_wrapper import browser_wrapper

from typing import Type, Optional, List

from pydantic import BaseModel, Field

from superagi.agent.agent_prompt_builder import AgentPromptBuilder
from superagi.tools.base_tool import BaseTool
from superagi.config.config import get_config
from superagi.llms.base_llm import BaseLlm
from pydantic import BaseModel, Field, PrivateAttr

class StartBrowserAndGoToPageSchema(BaseModel):
    url: str = Field(
        ...,
        description="The url of the website to navigate to",
    )

class StartBrowserAndGoToPageTool(BaseTool):
    name = "StartBrowserAndGoToPage"
    description = "A tool to start the Playwright browser after taking url as input and navigating to the url."

    args_schema: Type[StartBrowserAndGoToPageSchema] = StartBrowserAndGoToPageSchema

    def _execute(self, url: str) -> str:
        return browser_wrapper.start_browser_and_goto_page(url)