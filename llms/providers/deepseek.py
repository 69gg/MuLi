import json
from pydantic import BaseModel
import textwrap

def get_text_response(messages: list[dict], tools: None | list[dict], client, model_name) -> str:
    if tools:
        extra_body = {}
        if model_name == "deepseek-reasoner":
            extra_body["thinking"] = {"type": "enabled"}
            
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=tools,
            extra_body=extra_body
        )
        return response.choices[0].message
    else:
        completion = client.chat.completions.create(
            model=model_name,
            messages=messages
        )

        return completion.choices[0].message

def get_structure_output(messages: list[dict], text_format: BaseModel, client, model_name) -> dict:
    json_schema_dict = text_format.model_json_schema()
    json_schema = json.dumps(json_schema_dict, indent=4, ensure_ascii=False)
    schema_prompt_appendix = textwrap.dedent(f"""

    --- 结构化输出指令 ---
    请根据以上内容，严格遵循下面提供的 JSON Schema，
    生成一个**只包含** JSON 对象的回复。
    请勿添加任何解释性文字或代码块标记（如 ```json）。

    --- JSON SCHEMA START ---
    {json_schema}
    --- JSON SCHEMA END ---
    """)
    messages[len(messages) - 1]["content"] += schema_prompt_appendix
    response = client.chat.completions.create(
    model=model_name,
    messages=messages,
    response_format={
        'type': 'json_object'
        }
    )

    return json.loads(response.choices[0].message.content)
