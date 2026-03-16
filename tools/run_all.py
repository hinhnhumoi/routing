"""
Launch all nodes from config files in one Windows Terminal window with tabs.
Usage: python tools/run_all.py <RoutingDelay> <UpdateInterval>
Example: python tools/run_all.py 5 3
"""
import sys
import glob
import os


def main():
    routing_delay = sys.argv[1] if len(sys.argv) > 1 else "5"
    update_interval = sys.argv[2] if len(sys.argv) > 2 else "3"

    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'configs')
    config_files = sorted(glob.glob(os.path.join(config_dir, '*config.txt')))

    if not config_files:
        print("No config files found in configs/")
        sys.exit(1)

    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
    main_py = os.path.abspath(os.path.join(src_dir, 'main.py'))

    # Build wt command string
    parts = []
    for config in config_files:
        filename = os.path.basename(config)
        node_id = filename[0]
        port = 6000 + ord(node_id) - ord('A')
        config_abs = os.path.abspath(config)

        node_cmd = f'python \\"{main_py}\\" {node_id} {port} \\"{config_abs}\\" {routing_delay} {update_interval}'
        title = f"Node {node_id}"

        tab = f'new-tab --title "{title}" cmd.exe /k "{node_cmd}"'
        parts.append(tab)
        print(f"Tab: {title} on port {port}")

    wt_cmd = 'wt -w 0 ' + ' ; '.join(parts)
    os.system(wt_cmd)
    print(f"\nLaunched {len(config_files)} nodes in Windows Terminal!")


if __name__ == "__main__":
    main()
