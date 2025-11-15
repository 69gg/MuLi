## -!- START REGINSTER TOOL -!- ##
## -!- START TOOL DEFINITION -!- ##
TOOL_NAME = "base64"
TOOL_DESCRIPTION = "Decode or encode a given string using Base64 encoding."
TOOL_FUNCTIONS = ["encode_base64", "decode_base64"]
TOOL_PARAMETERS = [[{"string": "The string to be encoded or decoded using Base64."}], [{"string": "The string to be encoded or decoded using Base64."}]]
## -!- END TOOL DEFINITION -!- ##

import requests

def encode_base64(string: str) -> str:
    url = f"https://v2.xxapi.cn/api/base64?type=encode&text={string}"
    payload = {}
    headers = {
        'User-Agent': 'xiaoxiaoapi/1.0.0'
    }
    response = requests.request("GET", url, headers = headers, data = payload)
    return response.text

def decode_base64(string: str) -> str:
    url = f"https://v2.xxapi.cn/api/base64?type=decode&text={string}"
    payload = {}
    headers = {
        'User-Agent': 'xiaoxiaoapi/1.0.0'
    }
    response = requests.request("GET", url, headers = headers, data = payload)
    return response.text
## -!- END REGINSTER TOOL -!- ##
