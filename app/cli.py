from app import agent, db, scheduler


def main():
    db.init()
    scheduler.start()
    scheduler.restore_pending()
    print("sai (ctrl-d or 'exit' to quit)")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("exit", "quit"):
            return
        db.log_inbound(line)

        def emit(text: str):
            db.log_outbound(text)
            print(f"sai> {text}", flush=True)

        try:
            agent.handle(send=emit)
        except Exception as e:
            print(f"sai> [error: {e}]")


if __name__ == "__main__":
    try:
        main()
    finally:
        scheduler.shutdown()
