
## -!- START REGISTER TOOL -!- ##
## -!- START TOOL DEFINITION -!- ##
TOOL_NAME = "web_search"
TOOL_DESCRIPTION = "Search the web using searching engine like google and duckduckgo. If the tool return an url, you should use other tool to get the content."
TOOL_FUNCTIONS = ["web_search"]
TOOL_PARAMETERS = [[{"query": "The search query.", "max_results": "The maximum number of results to return.", "engine": "The search engine to use. Duckduckgo or Google"}]]
## -!- END TOOL DEFINITION -!- ##

from duckduckgo_search import DDGS
from googlesearch import search

def web_search(query: str, max_results: int = 5, engine: str = "google") -> str:
    if engine.upper == "GOOGLE":
        try:
            re = []
            for url in search(query, num_results=max_results):
                re.append(url)
            return "\n\n".join(re)
        except Exception as e:
            return f"搜索出错: {str(e)}"
    elif engine.upper == "DUCKDUCKGO":
        try:
            results = DDGS().text(query, max_results=max_results)
            
            if not results:
                return f"未找到关于 '{query}' 的结果。"
            
            formatted_results = []
            for r in results:
                title = r.get('title', '无标题')
                href = r.get('href', '#')
                body = r.get('body', '无摘要')
                formatted_results.append(f"标题: {title}\n链接: {href}\n摘要: {body}")
                
            return "\n\n".join(formatted_results)
            
        except Exception as e:
            return f"搜索出错: {str(e)}"
## -!- END REGISTER TOOL -!- ##