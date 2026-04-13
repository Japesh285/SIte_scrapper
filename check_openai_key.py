import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("❌ OPENAI_API_KEY not found in .env file")
    exit(1)

client = OpenAI(api_key=api_key)

try:
    client.models.list()
    print("✅ OpenAI API key is valid and active\n")
except Exception as e:
    print(f"❌ OpenAI API key is invalid or inactive: {e}")
    exit(1)

print("Terminal Chat (type 'quit' or 'exit' to end)\n" + "=" * 40)

messages = [{"role": "system", "content": "You are a helpful assistant."}]

while True:
    user_input = input("\nYou: ").strip()
    if user_input.lower() in ("quit", "exit"):
        print("Bye!")
        break
    if not user_input:
        continue

    messages.append({"role": "user", "content": user_input})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
        )
        reply = response.choices[0].message.content
        messages.append({"role": "assistant", "content": reply})
        print(f"\nAssistant: {reply}")
    except Exception as e:
        print(f"\n❌ Error: {e}")
