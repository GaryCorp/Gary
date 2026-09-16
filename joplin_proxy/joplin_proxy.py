"""Forward Joplin's Web Clipper API into the assistant's Docker network.

Joplin desktop only listens on the host's 127.0.0.1, which containers cannot
reach. This runs on the host network, listens only on the assistant network's
gateway address, and accepts connections only from that subnet.
"""

import asyncio
import ipaddress
import os


LISTEN_HOST = os.getenv("LISTEN_HOST", "172.30.99.1")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "41184"))
JOPLIN_PORT = int(os.getenv("JOPLIN_PORT", "41184"))
ALLOWED_SUBNET = ipaddress.ip_network(os.getenv("ALLOWED_SUBNET", "172.30.99.0/24"))


def close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        close(writer)


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    peer = client_writer.get_extra_info("peername")
    if not peer or ipaddress.ip_address(peer[0]) not in ALLOWED_SUBNET:
        close(client_writer)
        return

    try:
        joplin_reader, joplin_writer = await asyncio.open_connection(
            "127.0.0.1", JOPLIN_PORT
        )
    except OSError:
        # Joplin is not running; the backend reports it to the user.
        close(client_writer)
        return

    await asyncio.gather(
        pipe(client_reader, joplin_writer),
        pipe(joplin_reader, client_writer),
    )


async def main() -> None:
    # The gateway address exists only once Docker has created the network.
    while True:
        try:
            server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
            break
        except OSError as exc:
            print(
                f"[joplin-proxy] cannot listen on {LISTEN_HOST}:{LISTEN_PORT} "
                f"({exc}); retrying in 5 seconds",
                flush=True,
            )
            await asyncio.sleep(5)

    print(
        f"[joplin-proxy] forwarding {LISTEN_HOST}:{LISTEN_PORT} "
        f"to Joplin at 127.0.0.1:{JOPLIN_PORT}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
