from core.agents.MuLi import MuLi
ml = MuLi()
while True:
    user_input = input("> ")
    response = ml.chat(user_input)
    print(f"{response}")
    