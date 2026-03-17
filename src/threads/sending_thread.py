"""
Sending Thread - periodically broadcasts update packets via STDOUT and sockets.
Uses Link-State flooding: sends LSAs (own + all known) to direct neighbours.
"""
import threading
import time
import socket


class SendingThread(threading.Thread):
    def __init__(self, node):
        super().__init__()
        self.node = node
        self.last_update = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def run(self):
        while True:
            time.sleep(self.node.update_interval)
            self._send_update()

    def _send_update(self):
        """Broadcast update packet via STDOUT and flood all LSAs via sockets."""
        with self.node.lock:
            if self.node.is_down:
                return

            # Update own LSA before sending
            self.node._update_own_lsa()

            neighbours = dict(self.node.neighbours)

            # STDOUT packet: only 1-hop neighbours (per spec)
            parts = []
            for nb_id in sorted(neighbours.keys()):
                info = neighbours[nb_id]
                parts.append(f"{nb_id}:{info['cost']}:{info['port']}")
            packet = f"UPDATE {self.node.node_id} {','.join(parts)}"

            # Collect all LSAs to flood
            lsa_messages = []
            for origin, lsa in self.node.lsa_db.items():
                lsa_messages.append(self._encode_lsa(origin, lsa))

        # Print to STDOUT only if changed
        if packet != self.last_update:
            self.last_update = packet
            with self.node.print_lock:
                print(packet, flush=True)

        # Flood all LSAs to direct neighbours via UDP
        for nb_id, info in neighbours.items():
            for msg in lsa_messages:
                try:
                    self.sock.sendto(msg.encode('utf-8'), ('localhost', info['port']))
                except Exception:
                    pass

    def immediate_broadcast(self):
        """Immediately broadcast UPDATE to STDOUT and flood LSAs via sockets.
        Called directly from command handlers (CHANGE, RESET) — not from the periodic loop.
        """
        self._send_update()

    def flood_lsa(self, origin, lsa, exclude_port=None):
        """Forward a single LSA to all neighbours except the sender."""
        with self.node.lock:
            if self.node.is_down:
                return
            neighbours = dict(self.node.neighbours)

        msg = self._encode_lsa(origin, lsa)
        for nb_id, info in neighbours.items():
            if info['port'] == exclude_port:
                continue
            try:
                self.sock.sendto(msg.encode('utf-8'), ('localhost', info['port']))
            except Exception:
                pass

    @staticmethod
    def _encode_lsa(origin, lsa):
        """Encode LSA to string: LSA <origin> <seq> <nb1>:<cost>,<nb2>:<cost>,..."""
        seq = lsa['seq']
        nb_parts = []
        for nb_id in sorted(lsa['neighbours'].keys()):
            nb_parts.append(f"{nb_id}:{lsa['neighbours'][nb_id]}")
        nb_str = ",".join(nb_parts) if nb_parts else ""
        return f"LSA {origin} {seq} {nb_str}"
