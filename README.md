# Avoid Bot

Python bot for the Builders Mech Royale `mechfight/1` interface. It seeks cover,
stays inside the safe zone, gathers nearby loot, and delays firing until only
two mechs remain.

The official Python wrapper, protocol, schema, checker, and arena map are kept
at the repository root.

## Check the bot

From the repository root:

```sh
python3 check/check.py -- python3 -u avoid_bot.py
docker build -t mech-avoid-bot .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech-avoid-bot
```

The Docker image uses only Python's standard library and runs as an
unprivileged user with a read-only filesystem, matching the scoring runtime.
