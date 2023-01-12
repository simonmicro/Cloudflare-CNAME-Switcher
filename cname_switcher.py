import threading
from ipgetter2 import IPGetter
from urllib.request import Request, urlopen
import os
import json
import time
import ipaddress
import configparser
import datetime
import logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logger.debug('Booting...')

from http.server import HTTPServer, BaseHTTPRequestHandler

# Config stuff
config = configparser.ConfigParser(allow_no_value=True)
os.makedirs('config', exist_ok=True)
configPath = os.path.join('config', 'config.ini')
if os.path.exists(configPath) == False:
    config['Cloudflare'] = {
        '# Open the overview of the domain and look bottom-right...': None,
        'zone_id': '',
        '# Cloudflare account -> API-Token -> Create a new one with the Zone.DNS permission': None,
        'token': ''
    }
    config['General'] = {
        '# Enable this when something does not work...': None,
        'debug': 'false',
        '# This CNAME will by updated when the external ip leaves the primary subnet or enters the secondary subnet': None,
        'dynamic_cname': 'dyn.example.com',
        '# Update interval (warning: healthchecks expect < 1 minute). Please note the Client API are rate-limited by Cloudflare account to 1200 requests every 5 minutes': None,
        'update_interval': '30',
        '# We\'ll try to get the external ip from up to 3 servers, each with a time of x': None,
        'external_timeout': '10',
        '# You can here specify e.g. \'http://icanhazip.com/\' to enforce using only one specific resolver (in case the \'default\' are too unstable)...': None,
        'external_resolver': 'default',
        '# If you wanty you can add an healthchecks.io URI to get notified if the service crashes': None,
        'healthchecks_uri': ''
    }
    config['Telegram'] = {
        '# Set the bot token here (set to \'no\' to disable)': None,
        'token': 'no',
        '# Set the chat id here': None,
        'target': '10239482309'
    }
    config['DynDns'] = {
        '# A record to store the current IPv4 to (set to \'no\' to disable)': None,
        'dyndns_target': 'no',
        '# TTL to be applied to dyndns_target': None,
        'dyndns_ttl': '60'
    }
    config['Primary'] = {
        '# E.g. primary cable line': None,
        'CNAME': 'wan1.example.com',
        '# Commonly found by try-and-error - the following modes are supported:': None,
        '# - Only Primary.Subnet: Switch to primary when external IP enters it long enough.': None,
        '# - Only Secondary.Subnet: Switch to secondary when external IP enters it.': None,
        '# - Both subnets: Switch to primary when external IP enters it long enough and switch to secondary when external IP enters it. Otherwise do nothing.': None,
        'Subnet': '88.42.1.0/24',
        '# TTL to be applied to dynamic_cname when this is active': None,
        'TTL': '60',
        '# Amount of successful checks needed, until we switch back to primary from secondary': None,
        'Confidence': '4'
    }
    config['Secondary'] = {
        '# E.g. the fallback over mobile network': None,
        'CNAME': 'wan2.example.com',
        '# Commonly found by try-and-error (set to \'no\' to disable)': None,
        'Subnet': 'no',
        '# TTL to be applied to dynamic_cname when this is active (higher to prevent clients constantly switching when the network is bad)': None,
        'TTL': '300'
    }
    with open(configPath, 'w') as configfile:
        config.write(configfile)
        logger.info('Missing ' + configPath + ' -> written default one.')
        exit(0)
config.read(configPath)

if config.getboolean('General', 'debug'):
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.DEBUG, force=True)

def resolveNameToRecordId(config, name):
    request = Request(
        'https://api.cloudflare.com/client/v4/zones/' + config['Cloudflare']['zone_id'] + '/dns_records?name=' + name,
        method='GET',
        headers={
            'Authorization': 'Bearer ' + config['Cloudflare']['token'],
            'Content-Type': 'application/json'
            }
        )
    try:
        for dns in json.load(urlopen(request))['result']:
            if dns['name'] == name:
                logger.debug(name + ' is ' + dns['id'])
                return dns['id']
    except Exception as e:
        logger.exception('Failed to resolve ' + name + ' to record id: ' + str(e))
    return None

# Resolve the dynamic_cname to a dns entry id of Cloudflare
CloudflareDnsRecordId = resolveNameToRecordId(config, config['General']['dynamic_cname'])
if CloudflareDnsRecordId is None:
    logger.critical('Could not resolve ' + config['General']['dynamic_cname'] + ' to a Cloudflare dns id!')
    exit(1)
CloudflareDynDnsRecordId = None
if config['DynDns']['dyndns_target'] != 'no':
    CloudflareDynDnsRecordId = resolveNameToRecordId(config, config['DynDns']['dyndns_target'])
    if CloudflareDnsRecordId is None:
        logger.critical('Could not resolve ' + config['DynDns']['dyndns_target'] + ' to a Cloudflare dns id!')
        exit(2)

# Prepare the healthcheck endpoint
loopTime = int(config['General']['update_interval'])
class HealthcheckEndpoint(BaseHTTPRequestHandler):
    lastLoop = None

    def do_GET(self):
        self.protocol_version = 'HTTP/1.0'
        if not self.path.endswith('/healthz'):
            self.send_response(404)
            self.end_headers()
        else:
            okay = self.lastLoop is not None and datetime.datetime.now() - self.lastLoop < datetime.timedelta(seconds=loopTime * 2)
            msg = ('OK' if okay else 'BAD').encode('utf8')
            self.send_response(200 if okay else 503)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-length', len(msg))
            self.end_headers()
            self.wfile.write(msg)

healthcheckServer = HTTPServer(('0.0.0.0', 80), HealthcheckEndpoint)
healthcheckThread = threading.Thread(target=healthcheckServer.serve_forever)
healthcheckThread.daemon = True # Disconnect from main thread
healthcheckThread.start()

logger.info('Startup complete.')
getter = IPGetter()
getter.timeout = int(config['General']['external_timeout'])
primaryConfidence = int(int(config['Primary']['confidence']) / 2)
primaryActive = False
primarySubnetSet = config['Primary']['subnet'] != 'no'
secondarySubnetSet = config['Secondary']['subnet'] != 'no'
if primarySubnetSet:
    primarySubnet = ipaddress.ip_network(config['Primary']['subnet'])
if secondarySubnetSet:
    secondarySubnet = ipaddress.ip_network(config['Secondary']['subnet'])
bothSubnetSet = not (primarySubnetSet ^ secondarySubnetSet)
oldExternalIPv4 = None
externalIPv4 = None
ignoreFirstNotification = True
notificationBuffer = [] # In case sending a notification failes, it will be stored here...
try:
    def sendTelegramNotification(message, markdown):
        global ignoreFirstNotification, notificationBuffer, logger
        if ignoreFirstNotification:
            ignoreFirstNotification = False
            return
        if config['Telegram']['token'] == 'no':
            return
        try:
            req = Request('https://api.telegram.org/bot' + config['Telegram']['token'] + '/sendMessage', method='POST')
            req.add_header('Content-Type', 'application/json')
            data = { 'chat_id': config['Telegram']['target'] }
            if markdown:
                data['parse_mode'] = 'MarkdownV2'
                data['text'] = message.replace('.', '\\.')
            else:
                data['text'] = message
            data = json.dumps(data)
            data = data.encode()
            urlopen(req, timeout=10, data=data)
            logger.info('Sent Telegram notification successfully: ' + message)
            if len(notificationBuffer):
                retryThese = notificationBuffer
                notificationBuffer = [] # Empty current buffer
                logger.info(f'Processing {len(retryThese)} delayed massages...')
                for params in retryThese:
                    msg, markdown, timestamp = params
                    if markdown:
                        msg += f'\n\n_This is a delayed message from `{timestamp.isoformat()}`._'
                    else:
                        msg += f'\n\nThis is a delayed message from {timestamp.isoformat()}.'
                    try:
                        sendTelegramNotification(msg, markdown) # This will re-queue the message on failure...
                    except:
                        pass # Well... No.
        except Exception as e:
            notificationBuffer.append((message, markdown, datetime.datetime.now(datetime.timezone.utc)))
            logger.exception('Telegram notification error.')

    while True:
        # Get the external ip and validate primary cname allowance
        try:
            logger.debug('Resolving external IPv4...')
            if config['General']['external_resolver'] == 'default':
                externalIPv4 = ipaddress.ip_address(str(getter.get().v4))
            else:
                externalIPv4 = ipaddress.ip_address(str(getter.get_from(config['General']['external_resolver']).v4))
            
            # Update the cname to the external ip...
            if CloudflareDynDnsRecordId is not None and oldExternalIPv4 != externalIPv4:
                try:
                    data = {
                        'type': 'A',
                        'name': config['DynDns']['dyndns_target'],
                        'content': str(externalIPv4),
                        'ttl': config['DynDns']['dyndns_ttl'],
                        'proxied': False
                    }
                    urlopen(Request(
                        'https://api.cloudflare.com/client/v4/zones/' + config['Cloudflare']['zone_id'] + '/dns_records/' + CloudflareDynDnsRecordId,
                        method='PUT',
                        data=bytes(json.dumps(data), encoding='utf8'),
                        headers={
                            'Authorization': 'Bearer ' + config['Cloudflare']['token'],
                            'Content-Type': 'application/json'
                        }
                    ))
                    logger.info('Updated ' + config['DynDns']['dyndns_target'] + ' to ' + data['content'])
                    oldExternalIPv4 = externalIPv4
                except Exception as e:
                    logger.exception('Cloudflare A-record update error.')
                    sendTelegramNotification(f'Something went wrong at the Cloudflare A-record updater: {e}', False)
            
            externalIsPrimary = primarySubnetSet and externalIPv4 in primarySubnet
            externalIsSecondary = secondarySubnetSet and externalIPv4 in secondarySubnet
            if primarySubnetSet and externalIsPrimary or (secondarySubnetSet and not externalIsSecondary and not bothSubnetSet):
                primaryConfidence += 1
            elif secondarySubnetSet and externalIsSecondary or (primarySubnetSet and not externalIsPrimary and not bothSubnetSet):
                primaryConfidence = 0
            else:
                logger.warning('External IP (' + str(externalIPv4) + ') is in neither the primary (' + str(primarySubnet) + ') nor the secondary (' + str(secondarySubnet) + ') subnet -> ignoring...')
            logger.debug('External IP is ' + str(externalIPv4))
        except Exception as e:
            logger.exception('External IPv4 resolve error.')
            primaryConfidence = 0
            sendTelegramNotification(f'Something went wrong at the external IPv4 resolver: {e}', False)

        # And update the dns entry of Cloudflare...
        def updateDynamicCname(config, data) -> bool:
            try:
                urlopen(Request(
                    'https://api.cloudflare.com/client/v4/zones/' + config['Cloudflare']['zone_id'] + '/dns_records/' + CloudflareDnsRecordId,
                    method='PUT',
                    data=bytes(json.dumps(data), encoding='utf8'),
                    headers={
                        'Authorization': 'Bearer ' + config['Cloudflare']['token'],
                        'Content-Type': 'application/json'
                    }
                ))
                logger.info('Updated ' + config['General']['dynamic_cname'] + ' to ' + data['content'])
                return True
            except Exception as e:
                logger.exception('Cloudflare CNAME-record update error.')
                sendTelegramNotification(f'Something went wrong at the Cloudflare CNAME updater: {e}', False)
                return False

        if primaryConfidence == int(config['Primary']['confidence']) and not primaryActive:
            data = {
                'type': 'CNAME',
                'name': config['General']['dynamic_cname'],
                'content': config['Primary']['cname'],
                'ttl': int(config['Primary']['ttl']),
                'proxied': False
            }
            if updateDynamicCname(config, data):
                sendTelegramNotification(f'Primary network connection *STABLE* since `{primaryConfidence}` checks. Failover INACTIVE. Current IPv4 is `{externalIPv4}`.', True)
                primaryActive = True
        elif primaryConfidence == 0 and primaryActive:
            data = {
                'type': 'CNAME',
                'name': config['General']['dynamic_cname'],
                'content': config['Secondary']['cname'],
                'ttl': int(config['Secondary']['ttl']),
                'proxied': False
            }
            if updateDynamicCname(config, data):
                sendTelegramNotification(f'Primary network connection *FAILED*. Failover ACTIVE. Recheck in `{loopTime}` seconds... Current IPv4 is `{externalIPv4}`.', True)
                primaryActive = False
        logger.debug('primaryConfidence? ' + str(primaryConfidence))

        # Health check
        if config['General']['healthchecks_uri']:
            try:
                urlopen(config['General']['healthchecks_uri'])
            except:
                pass
        
        # Wait until next check...
        logger.debug('Sleeping...')
        HealthcheckEndpoint.lastLoop = datetime.datetime.now()
        time.sleep(loopTime)
except KeyboardInterrupt:
    pass
        
logger.info('Bye!')
healthcheckServer.shutdown() # stop the healthcheck server