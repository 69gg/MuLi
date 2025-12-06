## -!- START REGISTER TOOL -!- ##
## -!- START TOOL DEFINITION -!- ##
TOOL_NAME = "get_weather"
TOOL_DESCRIPTION = "Get the current weather information for a specified location."
TOOL_FUNCTIONS = ["get_weather_details"]
TOOL_PARAMETERS = [[{"city": "The name of the city to get the weather for."}]]
## -!- END TOOL DEFINITION -!- ##

import requests
from config_manage.manager import ConfigManager

config = ConfigManager("config.json")

def get_weather_details(city: str) -> str:
    url = f"https://v2.xxapi.cn/api/weatherDetails?city={city}&key={config.get("tools_api_config.get_weather.api_key")}"
    payload = {}
    headers = {
        'User-Agent': 'xiaoxiaoapi/1.0.0'
    }
    response = requests.request("GET", url, headers = headers, data = payload)
    return response.text
## -!- END REGISTER TOOL -!- ##
