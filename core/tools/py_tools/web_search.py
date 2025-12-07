
## -!- START REGISTER TOOL -!- ##
## -!- START TOOL DEFINITION -!- ##
TOOL_NAME = "web_search"
TOOL_DESCRIPTION = "Search the web using google. If the tool return an url, you should use other tool to get the content."
TOOL_FUNCTIONS = ["web_search"]
TOOL_PARAMETERS = [[{"query": "The search query.","engine": "The search engine to use, such as google, duckduckgo, github, etc. Default is google.", "max_length": "The maximum number of length to return in the response. Default is 3000."}]]
## -!- END TOOL DEFINITION -!- ##

from langchain_community.utilities import SearxSearchWrapper
from config_manage.manager import ConfigManager
config = ConfigManager()
if config.get("tools_api_config.web_search.enable") == True:
    searx_host = config.get("tools_api_config.web_search.base_url")
    s = SearxSearchWrapper(searx_host=searx_host)

s.run("what is a large language model?")
def web_search(query: str, engine: str = "google", max_length: int = 3000) -> str:
    if config.get("tools_api_config.web_search.enable") == False:
        return "Web search tool is disabled."
    result = s.run(query=query, engine=engine)
    return str(result)[:max_length] if len(str(result)) > max_length else str(result)
## -!- END REGISTER TOOL -!- ##