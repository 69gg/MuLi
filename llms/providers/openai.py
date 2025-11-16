def get_text_response(messages: list[dict], tools: None | list[dict], client, model_name) -> str:
    if tools:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=tools,
            timeout=600
        )
        return response.choices[0].message
    else:
        completion = client.chat.completions.create(
            model=model_name,
            messages=messages,
            timeout=600
        )

        return completion.choices[0].message

def get_structure_output(messages: list[dict], text_format, client, model_name) -> dict:
    response = client.responses.parse(
        model=model_name,
        input=messages,
        text_format=text_format,
        timeout=600
    )

    return response.output_parsed
