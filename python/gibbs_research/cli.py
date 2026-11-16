import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Gibbs research tools and preview")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    uvicorn.run("gibbs_research.server:create_app", factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
