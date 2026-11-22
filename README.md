# Oval Survivor

Python bot for the Builders Mech Royale `mechfight/1` interface. It prioritizes
staying inside the safe zone, moving unpredictably around nearby opponents,
leading arm-gun shots, and using found weapons and upgrades.

The official Python wrapper, protocol, schema, checker, and arena map are kept
at the repository root.

## Check the bot

From the repository root:

```sh
python3 check/check.py -- python3 -u example_bot.py
docker build -t oval-survivor .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" oval-survivor
```

The Docker image uses only Python's standard library and runs as an
unprivileged user with a read-only filesystem, matching the scoring runtime.
