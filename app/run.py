#!/usr/bin/env python3

import sys
import os
import time
import asyncio
import aiohttp
import json
import logging
import ipaddress
from configparser import ConfigParser

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1  # seconds; delay grows as RETRY_BACKOFF_BASE * 2**attempt

HEARTBEAT_FILE = os.path.join(os.path.dirname(__file__), '.last_success')


async def fetch_json(session, method, url, *, timeout, **kwargs):
    """Perform an HTTP request with a timeout and retry-with-backoff, returning parsed JSON.

    Retries on network errors, timeouts, 5xx responses, and 429 (rate limited).
    Any other 4xx response is raised immediately since retrying won't help.
    """
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs) as r:
                r.raise_for_status()
                return json.loads(await r.text())
        except aiohttp.ClientResponseError as exc:
            if exc.status != 429 and exc.status < 500:
                raise
            last_exc = exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc

        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            logging.debug(f"{method} {url} failed on attempt {attempt}/{RETRY_ATTEMPTS}: {last_exc}. Retrying in {delay}s")
            await asyncio.sleep(delay)

    raise last_exc


class Proxmox:
    def __init__(self, proxmox_url, proxmox_nodes, proxmox_token_name, proxmox_token, valid_networks,
                 predict_network, predict_ip_addresses, predict_ip_addresses_vmid_blacklist,
                 verify_ssl, request_timeout):
        self.proxmox_url = proxmox_url
        self.proxmox_nodes = proxmox_nodes
        self.proxmox_token = f"PVEAPIToken={proxmox_token_name}={proxmox_token}"
        self.valid_networks = valid_networks
        self.predict_network = predict_network
        self.predict_ip_addresses = predict_ip_addresses
        self.predict_ip_addresses_vmid_blacklist = predict_ip_addresses_vmid_blacklist
        self.ssl = None if verify_ssl else False
        self.request_timeout = request_timeout

    async def _get(self, session, url):
        return await fetch_json(session, 'GET', url, timeout=self.request_timeout,
                                 headers={"Authorization": self.proxmox_token}, ssl=self.ssl)

    async def get_vms(self):
        """Get VMs and LXCs from every configured Proxmox node.

        A single unreachable node, or a single malformed VM/LXC entry, does not abort
        the whole run - it is logged and the rest of the fleet is still processed.

        Returns False on total failure (no node could be listed at all). Otherwise returns
        (entities, roster, roster_complete):
          - entities: VMs/LXCs that resolved (or predicted) an IP address this cycle - the
            set that actually gets synced to Cloudflare. Can be empty even on a good cycle
            (e.g. every guest agent was briefly unresponsive).
          - roster: names of every VM/LXC Proxmox reported this cycle, from the list APIs,
            independent of whether an IP was resolved for them. This is the trustworthy
            "does this thing still exist at all" signal - use this, not `entities`, to decide
            whether a DNS record is safe to prune. An entity that merely failed IP resolution
            this cycle still appears here and must never be treated as gone.
          - roster_complete: True only if every configured node was listed successfully this
            cycle. False means the roster is missing whatever lives on the failed node(s), so
            it is unsafe to use for pruning this cycle.
        """
        async with aiohttp.ClientSession() as session:
            tasks = []
            roster = set()
            roster_complete = True
            any_node_ok = False
            for node in self.proxmox_nodes:
                logging.debug(f"Retrieving entities from node {node}...")
                try:
                    vms_data = await self._get(session, f"{self.proxmox_url}/api2/json/nodes/{node}/qemu")
                    vms = self._filter_vms(vms_data['data'])
                    roster.update(v['name'] for v in vms)
                    tasks.extend(asyncio.create_task(self.get_vm_ip(session, node, vm)) for vm in vms)

                    lxcs_data = await self._get(session, f"{self.proxmox_url}/api2/json/nodes/{node}/lxc")
                    lxcs = self._filter_vms(lxcs_data['data'])
                    roster.update(l['name'] for l in lxcs)
                    tasks.extend(asyncio.create_task(self.get_lxc_ip(session, node, lxc)) for lxc in lxcs)
                    any_node_ok = True
                except Exception:
                    logging.exception(f"Error listing VMs/LXCs on node {node}. Skipping this node for this cycle")
                    roster_complete = False

            if not any_node_ok:
                logging.error("No VMs or LXCs were discovered on any node")
                return False

            results = await asyncio.gather(*tasks, return_exceptions=True)
            entities = []
            for result in results:
                if isinstance(result, Exception):
                    logging.error("Unhandled error while processing an entity", exc_info=result)
                elif result:
                    entities.append(result)
            return entities, roster, roster_complete

    async def get_vm_ip(self, session, node, vm):
        vmid = vm['vmid']
        try:
            nic_info = await self.get_vm_nics(session, node, vmid)
            ip_address = self.get_ip_from_nics(nic_info) if nic_info else False
            if ip_address:
                vm['ip_address'] = str(ip_address)
                logging.info(f"IP address for {vmid} on {node} is {vm['ip_address']}")
            else:
                if not self._should_predict(vmid):
                    return None
                vm['ip_address'] = str(self.predict_network.network_address + int(vmid))
                logging.info(f"Unable to look up IP address for {vmid} on {node}. Using predicted address {vm['ip_address']}")
            return vm
        except Exception:
            logging.exception(f'Error while getting IP address for {vmid} on {node}')
            return None

    async def get_lxc_ip(self, session, node, lxc):
        vmid = lxc['vmid']
        try:
            ip_address = await self._get_lxc_static_ip(session, node, vmid)
            if not ip_address:
                # No static ip= in the config (DHCP, or no address configured at all). Ask
                # Proxmox for the container's actual live address instead of guessing - unlike
                # QEMU, LXC needs no guest agent for this since it shares the host kernel.
                ip_address = await self._get_lxc_live_ip(session, node, vmid)
            if ip_address:
                lxc['ip_address'] = str(ip_address)
                logging.info(f"IP address for {vmid} on {node} is {lxc['ip_address']}")
            else:
                if not self._should_predict(vmid):
                    return None
                lxc['ip_address'] = str(self.predict_network.network_address + int(vmid))
                logging.info(f"Unable to look up IP address for {vmid} on {node}. Using predicted address {lxc['ip_address']}")
            return lxc
        except Exception:
            logging.exception(f'Error while getting IP address for {vmid} on {node}')
            return None

    async def _get_lxc_static_ip(self, session, node, vmid):
        """Look up a statically configured IPv4 address for an LXC, validated against valid_networks.

        Interfaces are checked in name order (net0 before net1, etc.) and DHCP-configured
        interfaces (ip=dhcp) are skipped rather than treated as a literal IP.
        """
        try:
            data = await self._get(session, f"{self.proxmox_url}/api2/json/nodes/{node}/lxc/{vmid}/config")
        except Exception:
            logging.debug(f"Could not retrieve config for LXC {vmid} on {node}")
            return False

        config = data['data']
        net_keys = sorted((k for k in config if k.startswith('net') and k[3:].isdigit()), key=lambda k: int(k[3:]))
        for key in net_keys:
            value = config[key]
            if 'ip=' not in value:
                continue
            ip_part = value.split('ip=')[1].split(',')[0].split('/')[0]
            if ip_part.lower() == 'dhcp':
                continue
            try:
                ip = ipaddress.IPv4Address(ip_part)
            except ValueError:
                continue
            if any(ip in n for n in self.valid_networks):
                return ip
            logging.debug(f"Static IP {ip} for LXC {vmid} on {node} (interface {key}) is outside valid_networks, ignoring")
        return False

    async def _get_lxc_live_ip(self, session, node, vmid):
        """Look up the LXC's actual live IPv4 address, validated against valid_networks.

        Covers DHCP-assigned addresses (and anything else not visible in the static config),
        by reading the container's live network interfaces directly - LXC needs no guest agent
        for this, unlike QEMU, since containers share the host kernel. Requires the container
        to be running, and requires a Proxmox version that has this endpoint (added in PVE 8);
        either condition failing just means no address is found here, and the caller falls
        through to prediction exactly as it would for a stopped or agent-less QEMU VM.
        """
        try:
            data = await self._get(session, f"{self.proxmox_url}/api2/json/nodes/{node}/lxc/{vmid}/interfaces")
        except Exception as exc:
            logging.debug(f"Could not query live interfaces for LXC {vmid} on {node}: {exc}")
            return False

        for interface in data.get('data') or []:
            for ipaddr in interface.get('ip-addresses', []):
                if ipaddr.get('ip-address-type') != 'inet':
                    continue
                try:
                    ip = ipaddress.IPv4Address(ipaddr['ip-address'])
                except ValueError:
                    continue
                if any(ip in n for n in self.valid_networks):
                    return ip
                logging.debug(f"Live IP {ip} for LXC {vmid} on {node} (interface {interface.get('name')}) "
                               f"is outside valid_networks, ignoring")
        return False

    async def get_vm_nics(self, session, node, vmid):
        try:
            data = await self._get(session, f"{self.proxmox_url}/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
            results = data['data']['result']
            if 'error' in results:
                logging.debug(f"Guest agent returned an error for VM {vmid} on {node}: {results['error']}")
                return False
            return results
        except Exception as exc:
            logging.debug(f"Could not query guest agent for VM {vmid} on {node}: {exc}")
            return False

    def get_ip_from_nics(self, nic_info):
        for interface in nic_info:
            if 'ip-addresses' in interface:
                for ipaddr in interface["ip-addresses"]:
                    if ipaddr.get('ip-address-type') == 'ipv4':
                        ip = ipaddress.IPv4Address(ipaddr['ip-address'])
                        if any(ip in n for n in self.valid_networks):
                            return ip
                        logging.debug(f"Discovered IP {ip} is outside valid_networks, ignoring")
        return False

    def _should_predict(self, vmid):
        if not self.predict_ip_addresses:
            logging.debug(f"IP prediction is disabled; not predicting an IP for {vmid}")
            return False
        if str(vmid) in self.predict_ip_addresses_vmid_blacklist:
            logging.debug(f"VMID {vmid} is on the prediction blacklist; not predicting an IP")
            return False
        # last usable host offset in predict_network (excludes network and broadcast addresses)
        max_offset = self.predict_network.num_addresses - 2
        if int(vmid) > max_offset:
            logging.debug(f"VMID {vmid} exceeds predict_network's usable host range (max {max_offset}); cannot predict an IP")
            return False
        return True

    def _filter_vms(self, entities):
        return [{k: v for k, v in d.items() if k in ('name', 'vmid')} for d in entities if d.get('template') != 1]


class Cloudflare:
    """Manages Cloudflare DNS A records for one zone.

    Use as an async context manager so the underlying HTTP session is reused across all
    requests for a run and closed cleanly afterward: `async with Cloudflare(...) as cf:`.
    """

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, cloudflare_token, cloudflare_zone_name, request_timeout):
        self.cloudflare_token = cloudflare_token
        self.cloudflare_zone_name = cloudflare_zone_name
        self.request_timeout = request_timeout
        self.session = None
        self.zone_id = None
        self.zone_records = {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.cloudflare_token}"})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def _api(self, method, path, **kwargs):
        return await fetch_json(self.session, method, f"{self.BASE_URL}{path}", timeout=self.request_timeout, **kwargs)

    async def setup(self):
        """Look up the zone id and existing A records. Must be called before update_record/delete_record."""
        self.zone_id = await self._lookup_zone_id()
        if not self.zone_id:
            logging.error("No zone id found")
            return False

        self.zone_records = await self._get_records() or {}
        if not self.zone_records:
            logging.warning("No A records found in this zone. Proceeding, assuming the zone is empty")

        return True

    async def update_record(self, record_name, ip_address):
        """Create or update an A record so it points at ip_address."""
        existing = self.zone_records.get(record_name)
        if existing and existing['ip_address'] == ip_address:
            logging.info(f"Skipping update of {record_name}. It is already in the desired state")
            return

        if existing:
            logging.debug(f"Record found, updating {record_name} with {ip_address}")
            if await self._update_record(record_name, existing['record_id'], ip_address):
                logging.info(f"Updated record for {record_name} ({ip_address})")
        else:
            logging.debug(f"Record not found, creating {record_name} with {ip_address}")
            if await self._create_record(record_name, ip_address):
                logging.info(f"Created record for {record_name} ({ip_address})")

    async def delete_record(self, record_name, record_id):
        """Delete a stale A record that no longer matches any known VM/LXC."""
        try:
            await self._api('DELETE', f"/zones/{self.zone_id}/dns_records/{record_id}")
            logging.warning(f"Deleted stale DNS record {record_name} ({record_id}); no matching VM/LXC was found")
            return True
        except Exception:
            logging.exception(f"Failed to delete stale record {record_name} ({record_id})")
            return False

    async def _lookup_zone_id(self):
        try:
            result = await self._api('GET', f"/zones?name={self.cloudflare_zone_name}")
            zone_id = result['result'][0]['id']
            logging.debug(f"Zone ID lookup finished: {zone_id}")
            return zone_id
        except Exception:
            logging.exception(f"Failed to look up zone id for {self.cloudflare_zone_name}")
            return None

    async def _get_records(self):
        try:
            total_pages, records = await self._get_records_page(1)
            for page in range(2, total_pages + 1):
                _, more = await self._get_records_page(page)
                records.update(more)
            logging.debug(f"Records lookup completed. Found {len(records)} total records")
            return records
        except Exception:
            logging.exception(f"Failed to retrieve records for zone {self.cloudflare_zone_name}")
            return None

    async def _get_records_page(self, page):
        result = await self._api('GET', f"/zones/{self.zone_id}/dns_records?type=A&per_page=100&page={page}")
        total_pages = result['result_info']['total_pages']
        records = {}
        for rec in result['result']:
            if rec['name'] in records:
                logging.warning(f"Multiple A records found for {rec['name']} in this zone; only the last one "
                                 f"encountered (id {rec['id']}) will be managed")
            records[rec['name']] = {'ip_address': rec['content'], 'record_id': rec['id']}
        logging.debug(f"Records lookup completed for page {page} of {total_pages}. Found {len(records)} records")
        return total_pages, records

    async def _create_record(self, record_name, ip_address):
        try:
            payload = {"type": "A", "name": record_name, "content": ip_address, "ttl": 120, "proxied": False}
            result = await self._api('POST', f"/zones/{self.zone_id}/dns_records", json=payload)
            record_id = result['result']['id']
            logging.debug(f"Record {record_name} created with {ip_address}, record ID {record_id}")
            return record_id
        except Exception:
            logging.exception(f"Failed to create record for {record_name} ({ip_address})")
            return None

    async def _update_record(self, record_name, record_id, ip_address):
        try:
            payload = {"type": "A", "name": record_name, "content": ip_address, "ttl": 120, "proxied": False}
            result = await self._api('PUT', f"/zones/{self.zone_id}/dns_records/{record_id}", json=payload)
            logging.debug(f"Record {record_name} updated to {ip_address}, record ID {result['result']['id']}")
            return result['result']['id']
        except Exception:
            logging.exception(f"Failed to update record for {record_name} ({ip_address})")
            return None


async def sync_to_cloudflare(cloudflare_token, cloudflare_zone, cloudflare_dns_subdomain, request_timeout,
                              prune_stale_records, vms, roster, roster_complete):
    async with Cloudflare(cloudflare_token, cloudflare_zone, request_timeout) as cf:
        if not await cf.setup():
            logging.error("Failed to set up the Cloudflare zone. Skipping this sync cycle")
            return

        managed_suffix = f".{cloudflare_dns_subdomain}.{cloudflare_zone}" if cloudflare_dns_subdomain else f".{cloudflare_zone}"

        def to_fqdn(name):
            return f"{name.removesuffix(managed_suffix)}{managed_suffix}"

        expected_names = set()
        tasks = []
        for vm in vms:
            fqdn = to_fqdn(vm['name'])
            if fqdn in expected_names:
                logging.warning(f"Multiple VMs/LXCs resolve to the DNS name {fqdn}; only one of their IP "
                                 f"addresses will end up set, and which one wins is not guaranteed")
            expected_names.add(fqdn)
            tasks.append(asyncio.create_task(cf.update_record(fqdn, vm['ip_address'])))
        await asyncio.gather(*tasks, return_exceptions=True)

        if not roster_complete:
            logging.info("Skipping the stale-record check this cycle: not every Proxmox node could be listed, "
                         "so the current VM/LXC roster is incomplete and unsafe to prune against")
            return

        roster_fqdns = {to_fqdn(name) for name in roster}
        await _prune_stale_records(cf, managed_suffix, roster_fqdns, prune_stale_records)


async def _prune_stale_records(cf, managed_suffix, roster_fqdns, enabled):
    """Remove (or report) A records under our managed suffix with no matching entry in the
    current Proxmox roster - i.e. a VM/LXC that has genuinely been removed or renamed.

    Checked against the full node roster, not just entities that resolved an IP this cycle -
    an entity that merely failed IP resolution this cycle (guest agent hiccup, transient LXC
    config-read failure, etc.) still exists in the roster and must never be treated as stale.

    Disabled by default: deleting DNS records is destructive, and without CLOUDFLARE_DNS_SUBDOMAIN
    scoping this can match unrelated hand-created records that merely share the zone.
    """
    stale = [(name, info['record_id']) for name, info in cf.zone_records.items()
             if name.endswith(managed_suffix) and name not in roster_fqdns]
    if not stale:
        return

    if not enabled:
        names = ', '.join(name for name, _ in stale)
        logging.info(f"{len(stale)} stale DNS record(s) with no matching VM/LXC found: {names}. "
                      f"Set PRUNE_STALE_RECORDS=true to remove them automatically.")
        return

    tasks = [asyncio.create_task(cf.delete_record(name, record_id)) for name, record_id in stale]
    await asyncio.gather(*tasks, return_exceptions=True)


def setup_logging(debug):
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if debug else logging.INFO)


def _get_str(config, section, option, default=None):
    # envsubst always leaves the key present in config.ini, just blank when the env var is
    # unset, so a plain ConfigParser fallback= never actually triggers for optional fields.
    value = config.get(section, option, fallback='').strip()
    return value if value else default


def _get_bool(config, section, option, default):
    value = _get_str(config, section, option)
    return value.lower() in ('true', '1', 'yes', 'on') if value is not None else default


def _get_int(config, section, option, default):
    value = _get_str(config, section, option)
    return int(value) if value is not None else default


def _record_success():
    with open(HEARTBEAT_FILE, 'w') as f:
        f.write(str(int(time.time())))


try:
    # interpolation=None: config.ini values (tokens, URLs) come from arbitrary env vars via
    # envsubst and must be read literally. The default interpolation treats a bare "%" as the
    # start of a %(name)s reference and raises InterpolationSyntaxError on a value that has one.
    config = ConfigParser(interpolation=None)
    if not config.read(os.path.join(os.path.dirname(__file__), 'config.ini')):
        raise FileNotFoundError("config.ini not found")

    debug = _get_bool(config, 'main', 'debug', False)
    setup_logging(debug)

    request_timeout = _get_int(config, 'main', 'request_timeout', 30)

    predict_network_raw = _get_str(config, 'main', 'predict_network')
    if not predict_network_raw:
        raise ValueError("predict_network must be set")
    predict_network = ipaddress.IPv4Network(predict_network_raw)
    if predict_network.num_addresses == 1:
        raise ValueError(f"You must give a network in X.X.X.X/Y format. Got {predict_network}")

    valid_networks_raw = _get_str(config, 'main', 'valid_networks')
    if not valid_networks_raw:
        raise ValueError("valid_networks must be set")
    valid_networks = [ipaddress.IPv4Network(n.strip()) for n in valid_networks_raw.split(',') if n.strip()]
    for net in valid_networks:
        if net.num_addresses == 1:
            raise ValueError(f"You must give a network in X.X.X.X/Y format. Got {net}")

    predict_ip_addresses = _get_bool(config, 'main', 'predict_ip_addresses', False)
    predict_ip_addresses_vmid_blacklist = [
        v.strip() for v in _get_str(config, 'main', 'predict_ip_addresses_vmid_blacklist', '').split(',') if v.strip()
    ]

    proxmox_url = _get_str(config, 'proxmox', 'proxmox_url')
    proxmox_nodes = [n.strip() for n in _get_str(config, 'proxmox', 'proxmox_nodes', '').split(',') if n.strip()]
    proxmox_token_name = _get_str(config, 'proxmox', 'proxmox_token_name')
    proxmox_token = _get_str(config, 'proxmox', 'proxmox_token')
    proxmox_verify_ssl = _get_bool(config, 'proxmox', 'proxmox_verify_ssl', False)
    if not (proxmox_url and proxmox_token_name and proxmox_token and proxmox_nodes):
        raise ValueError("proxmox_url, proxmox_nodes, proxmox_token_name, and proxmox_token are all required")

    cloudflare_token = _get_str(config, 'cloudflare', 'cloudflare_token')
    cloudflare_zone = _get_str(config, 'cloudflare', 'cloudflare_zone')
    cloudflare_dns_subdomain = _get_str(config, 'cloudflare', 'cloudflare_dns_subdomain')
    prune_stale_records = _get_bool(config, 'cloudflare', 'prune_stale_records', False)
    if not (cloudflare_token and cloudflare_zone):
        raise ValueError("cloudflare_token and cloudflare_zone are required")

    if not proxmox_verify_ssl:
        logging.warning("TLS certificate verification is disabled for Proxmox API requests "
                         "(PROXMOX_VERIFY_SSL=false). Set it to true once your Proxmox API has a trusted certificate.")
except FileNotFoundError as err:
    logging.exception(f"Unable to read config file! Error: {err}")
    sys.exit(1)
except ValueError as err:
    logging.exception(f"Invalid configuration. Error: {err}")
    sys.exit(1)
except Exception as err:
    logging.exception(f"Unable to parse config.ini or missing settings! Error: {err}")
    sys.exit(1)

proxmox = Proxmox(proxmox_url, proxmox_nodes, proxmox_token_name, proxmox_token, valid_networks,
                   predict_network, predict_ip_addresses, predict_ip_addresses_vmid_blacklist,
                   proxmox_verify_ssl, request_timeout)

result = asyncio.run(proxmox.get_vms())

if result is False:
    logging.critical("Unable to get VM/LXC list from Proxmox")
    sys.exit(1)

vms, roster, roster_complete = result
if not vms:
    logging.warning("No VMs or LXCs with a resolvable or predictable IP address were found this cycle")

asyncio.run(sync_to_cloudflare(cloudflare_token, cloudflare_zone, cloudflare_dns_subdomain,
                                request_timeout, prune_stale_records, vms, roster, roster_complete))

_record_success()
