"""Install a TM2020 or ManiaPlanet dedicated server instance."""
from __future__ import annotations

import secrets
import stat
from pathlib import Path
from typing import Callable

from .. import config, downloads, paths, ports
from ..instances.server import GameType, ServerMeta

Log = Callable[[str], None]


_DEDICATED_CFG = """<?xml version="1.0" encoding="utf-8" ?>
<dedicated>
  <authorization_levels>
    <level><name>SuperAdmin</name><password>{super_pw}</password></level>
    <level><name>Admin</name><password>{admin_pw}</password></level>
    <level><name>User</name><password>{user_pw}</password></level>
  </authorization_levels>

  <masterserver_account>
    <login></login>
    <password></password>
    <validation_key></validation_key>
  </masterserver_account>

  <server_options>
    <name>{server_name}</name>
    <comment>Managed by tmsm</comment>
    <hide_server>0</hide_server>
    <max_players>32</max_players>
    <password></password>
    <password_spectator></password_spectator>
    <max_spectators>32</max_spectators>
    <keep_player_slot>1</keep_player_slot>
    <enable_p2p_upload>1</enable_p2p_upload>
    <enable_p2p_download>1</enable_p2p_download>
    <ladder_mode>forced</ladder_mode>
    <ladder_serverlimit_min>0</ladder_serverlimit_min>
    <ladder_serverlimit_max>0</ladder_serverlimit_max>
    <enable_callvote>1</enable_callvote>
    <callvote_ratio>0.5</callvote_ratio>
    <callvote_timeout>60000</callvote_timeout>
    <allow_map_download>1</allow_map_download>
    <autosave_replays>0</autosave_replays>
    <autosave_validation_replays>0</autosave_validation_replays>
    <referee_password></referee_password>
    <referee_mode>0</referee_mode>
    <use_changing_validation_seed>0</use_changing_validation_seed>
    <disable_horns>0</disable_horns>
    <clientinputs_maxlatency>0</clientinputs_maxlatency>
    <disable_replay_recording>0</disable_replay_recording>
  </server_options>

  <system_config>
    <connection_uploadrate>102400</connection_uploadrate>
    <connection_downloadrate>102400</connection_downloadrate>
    <packetassembly_packetsperframe>0</packetassembly_packetsperframe>
    <packetassembly_fullpacketsperframe>0</packetassembly_fullpacketsperframe>
    <delayed_visuals>0</delayed_visuals>
    <trustclientsimu_ratio>0.0</trustclientsimu_ratio>
    <bind_ip></bind_ip>
    <server_port>{game_port}</server_port>
    <server_p2p_port>{game_port}</server_p2p_port>
    <client_port>0</client_port>
    <client_p2p_port>0</client_p2p_port>
    <use_nat_upnp>0</use_nat_upnp>
    <xmlrpc_port>{xmlrpc_port}</xmlrpc_port>
    <xmlrpc_allowremote>0</xmlrpc_allowremote>
    <title></title>
    <force_ip_address></force_ip_address>
    <proxy_global></proxy_global>
    <proxy_for_blacklist></proxy_for_blacklist>
    <blacklist_url></blacklist_url>
    <guestlist_filename></guestlist_filename>
    <blacklist_filename></blacklist_filename>
  </system_config>
</dedicated>
"""


_MATCH_SETTINGS = """<?xml version="1.0" encoding="utf-8" ?>
<playlist>
  <gameinfos>
    <game_mode>0</game_mode>
    <chat_time>10000</chat_time>
    <finishtimeout>1</finishtimeout>
    <allwarmupduration>0</allwarmupduration>
    <disablerespawn>0</disablerespawn>
    <forceshowallopponents>0</forceshowallopponents>
    <script_name>TrackMania/TM_TimeAttack_Online.Script.txt</script_name>
  </gameinfos>
  <hotseat>
    <game_mode>0</game_mode>
    <time_limit>300000</time_limit>
    <rounds_count>5</rounds_count>
  </hotseat>
  <filter>
    <is_lan>1</is_lan>
    <is_internet>1</is_internet>
    <is_solo>0</is_solo>
    <is_hotseat>0</is_hotseat>
    <sort_index>1000</sort_index>
    <random_map_order>0</random_map_order>
    <force_default_gamemode>0</force_default_gamemode>
  </filter>
  <script_settings>
    <setting name="S_TimeLimit"   type="integer" value="360"/>
    <setting name="S_WarmUpNb"    type="integer" value="0"/>
    <setting name="S_WarmUpDuration" type="integer" value="0"/>
  </script_settings>
{map_entries}
</playlist>
"""


def install_server(name: str, game: GameType, log: Log) -> Path:
    cfg = config.load()
    url = cfg.downloads.tm2020_url if game is GameType.TM2020 else cfg.downloads.maniaplanet_url
    binary = "TrackmaniaServer" if game is GameType.TM2020 else "ManiaPlanetServer"
    title = "Trackmania" if game is GameType.TM2020 else "TMCanyon"

    root = paths.SERVERS_DIR / name
    if root.exists():
        raise FileExistsError(f"Server '{name}' already exists at {root}")

    game_port, xmlrpc_port = ports.allocate_server_ports()
    log(f"Allocated ports: game={game_port}  xmlrpc={xmlrpc_port}")

    server_dir = root / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)

    cache_dir = paths.HOME / "cache"
    zip_path = cache_dir / f"{game.value}-server.zip"
    if zip_path.exists():
        log(f"Using cached download: {zip_path}")
    else:
        downloads.download(url, zip_path, log)
    downloads.extract_zip(zip_path, server_dir, log)

    bin_path = server_dir / binary
    if bin_path.exists():
        mode = bin_path.stat().st_mode
        bin_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        log(f"chmod +x {binary}")
    else:
        log(f"WARN: expected binary '{binary}' not found at {bin_path}")

    cfg_dir = server_dir / "UserData" / "Config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    super_pw = secrets.token_urlsafe(16)
    admin_pw = secrets.token_urlsafe(12)
    user_pw = secrets.token_urlsafe(12)
    (cfg_dir / "dedicated_cfg.txt").write_text(_DEDICATED_CFG.format(
        server_name=name,
        super_pw=super_pw, admin_pw=admin_pw, user_pw=user_pw,
        game_port=game_port, xmlrpc_port=xmlrpc_port,
    ))
    log("Wrote dedicated_cfg.txt (passwords generated)")

    # Match settings (maplist). Required for TM2020 server to start successfully.
    # We auto-populate with any .Map.Gbx already present under UserData/Maps.
    maps_dir = server_dir / "UserData" / "Maps"
    ms_dir = maps_dir / "MatchSettings"
    ms_dir.mkdir(parents=True, exist_ok=True)
    map_entries = []
    if maps_dir.is_dir():
        for p in sorted(maps_dir.rglob("*.Map.Gbx")):
            try:
                rel = p.relative_to(maps_dir)
            except ValueError:
                continue
            # Skip maps that live under MatchSettings itself.
            if rel.parts and rel.parts[0] == "MatchSettings":
                continue
            map_entries.append(f"  <map><file>{rel.as_posix()}</file></map>")
    maplist = ms_dir / "example.txt"
    maplist.write_text(_MATCH_SETTINGS.format(map_entries="\n".join(map_entries)))
    if map_entries:
        log(f"Wrote MatchSettings/example.txt with {len(map_entries)} map(s)")
    else:
        log("Wrote MatchSettings/example.txt (no maps found yet — add .Map.Gbx "
            "files under UserData/Maps/ and edit the maplist via the config editor)")

    ServerMeta(
        name=name, game=game,
        game_port=game_port, xmlrpc_port=xmlrpc_port,
        title=title, binary=binary,
    ).save(root)
    log(f"Server '{name}' installed at {root}")
    log(f"  SuperAdmin password: {super_pw}")
    return root


def update_server(name: str, log: Log) -> Path:
    """Re-download the dedicated server zip and overwrite engine files.

    UserData/ (configs, maps, scripts) is preserved.
    """
    cfg = config.load()
    root = paths.SERVERS_DIR / name
    if not root.exists():
        raise FileNotFoundError(f"Server '{name}' not found at {root}")
    meta = ServerMeta.load(root)
    url = cfg.downloads.tm2020_url if meta.game is GameType.TM2020 else cfg.downloads.maniaplanet_url

    server_dir = root / "server"
    user_data = server_dir / "UserData"

    cache_dir = paths.HOME / "cache"
    zip_path = cache_dir / f"{meta.game.value}-server-update.zip"
    if zip_path.exists():
        zip_path.unlink()
    downloads.download(url, zip_path, log)

    # Extract into a temp dir, then merge over server_dir while skipping UserData.
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tmsm-update-") as td:
        tmp = Path(td)
        downloads.extract_zip(zip_path, tmp, log)
        log("Merging new files (UserData preserved)...")
        for src in tmp.rglob("*"):
            rel = src.relative_to(tmp)
            # Skip the entire UserData tree from the archive
            if rel.parts and rel.parts[0] == "UserData":
                continue
            dst = server_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    bin_path = server_dir / meta.binary
    if bin_path.exists():
        mode = bin_path.stat().st_mode
        bin_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        log(f"chmod +x {meta.binary}")

    log(f"Server '{name}' updated.")
    return root


def delete_server(name: str, log: Log) -> None:
    """Remove a server instance directory."""
    import shutil
    root = paths.SERVERS_DIR / name
    if not root.exists():
        raise FileNotFoundError(f"Server '{name}' not found at {root}")
    log(f"Removing {root}")
    shutil.rmtree(root)
    log(f"Server '{name}' deleted.")
