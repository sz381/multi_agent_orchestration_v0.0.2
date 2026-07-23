import asyncio

from orchestration.graph import build_graph


async def main():
    graph = build_graph()

    result = await graph.ainvoke({
        "messages": [],
        "user_query": "你好，介绍一下你自己",
        "conversation_id": "test_001",
        "orchestration_id": "test_001",
        "plan": None,
        "sub_agent_round_tasks": [],
        "sub_agent_outputs": {},
        "orchestration_status": "",
        "should_orchestration_pause": False,
        "should_orchestration_stop": False,
        "response": "",
        "output_artifacts": [],
        "total_tokens": 0,
        "start_at": "",
        "time_elapsed": 0.0,
        "error_message": "",
    })

    print("=" * 50)
    print(result["response"])
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
    