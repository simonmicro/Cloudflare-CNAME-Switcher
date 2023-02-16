import threading
from ipgetter2 import IPGetter
from urllib.request import Request, urlopen
import json
import time
import ipaddress
import yaml
import datetime
import argparse
import logging
import sys
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logger.debug('Booting...')

from http.server import HTTPServer, BaseHTTPRequestHandler

parser = argparse.ArgumentParser()
parser.add_argument('--config', '-c', type=str, required=True, help='Path to the configuration file')
parser.add_argument('--debug', '-d', action='store_true', help='Something does not work? Debug mode!')
args = parser.parse_args()

# Config stuff
with open(args.config, 'r') as configFile:
    config = yaml.safe_load(configFile)

if args.debug:
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.DEBUG, force=True)

# Stuff, which should be set, when the user is not using the sample-config anymore...
assert config['cloudflare']['zone_id'], 'cloudflare.zone_id should be given'
assert config['cloudflare']['token'], 'cloudflare.token should be given'
assert config['general']['dynamic_cname'], 'general.dynamic_cname should be given'
assert config['primary']['cname'], 'primary.cname should be given'
assert config['secondary']['cname'], 'secondary.cname should be given'
assert len(config['primary']['subnets']) > 0 or len(config['secondary']['subnets']) > 0, 'primary or secondary subnets should be given'

def resolveNameToRecordId(config, name):
    request = Request(
        'https://api.cloudflare.com/client/v4/zones/' + config['cloudflare']['zone_id'] + '/dns_records?name=' + name,
        method='GET',
        headers={
            'Authorization': 'Bearer ' + config['cloudflare']['token'],
            'Content-Type': 'application/json'
            }
        )
    for dns in json.load(urlopen(request))['result']:
        if dns['name'] == name:
            logger.debug(name + ' is ' + dns['id'])
            return dns['id']

# Resolve the dynamic_cname to a dns entry id of Cloudflare
try:
    CloudflareDnsRecordId = resolveNameToRecordId(config, config['general']['dynamic_cname'])
except:
    logger.exception('Could not resolve ' + config['general']['dynamic_cname'] + ' to a Cloudflare dns id!')
    sys.exit(1)
CloudflareDynDnsRecordId = None
if config['dyndns']['dyndns_target']:
    try:
        CloudflareDynDnsRecordId = resolveNameToRecordId(config, config['dyndns']['dyndns_target'])
    except:
        logger.exception('Could not resolve ' + config['dyndns']['dyndns_target'] + ' to a Cloudflare dns id!')
        sys.exit(2)

# Prepare the healthcheck endpoint
loopTime = config['general']['update_interval']
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

    def log_message(self, format, *args):
        # Do not print the healthcheck requests to the console!
        return

healthcheckServer = HTTPServer(('0.0.0.0', 80), HealthcheckEndpoint)
healthcheckThread = threading.Thread(target=healthcheckServer.serve_forever)
healthcheckThread.daemon = True # Disconnect from main thread
healthcheckThread.start()

# Configure the ipgetter
getter = IPGetter()
getter.timeout = config['general']['external_timeout']

primaryConfidence = int(config['primary']['confidence'] / 2)
primaryActive = False
primarySubnets = [ipaddress.ip_network(n) for n in config['primary']['subnets']]
secondarySubnets = [ipaddress.ip_network(n) for n in config['secondary']['subnets']]
bothSubnetsSet = len(primarySubnets) > 0 and len(secondarySubnets) > 0
telegramToken = config['telegram']['token']
telegramTarget = config['telegram']['target']
if telegramToken is not None:
    assert telegramTarget, 'telegram.target should be given'

logger.info('Startup complete.')
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
        if telegramToken is None:
            return
        try:
            req = Request('https://api.telegram.org/bot' + telegramToken + '/sendMessage', method='POST')
            req.add_header('Content-Type', 'application/json')
            data = { 'chat_id': telegramTarget }
            if markdown:
                data['parse_mode'] = 'MarkdownV2'
                data['text'] = message.replace('.', '\\.')
            else:
                data['text'] = message
            data = json.dumps(data)
            data = data.encode()
            urlopen(req, timeout=10, data=data)
            logger.info('Sent Telegram notification successfully: ' + message.replace('\n', ' '))
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
        except:
            notificationBuffer.append((message, markdown, datetime.datetime.now(datetime.timezone.utc)))
            logger.exception('Telegram notification error.')

    while True:
        # Get the external ip and validate primary cname allowance
        try:
            logger.debug('Resolving external IPv4...')
            if config['general']['external_resolver'] == 'default':
                externalIPv4 = ipaddress.ip_address(str(getter.get().v4))
            else:
                externalIPv4 = ipaddress.ip_address(str(getter.get_from(config['general']['external_resolver']).v4))
            
            # Update the cname to the external ip...
            if CloudflareDynDnsRecordId is not None and oldExternalIPv4 != externalIPv4:
                try:
                    data = {
                        'type': 'A',
                        'name': config['dyndns']['dyndns_target'],
                        'content': str(externalIPv4),
                        'ttl': config['dyndns']['dyndns_ttl'],
                        'proxied': False
                    }
                    urlopen(Request(
                        'https://api.cloudflare.com/client/v4/zones/' + config['cloudflare']['zone_id'] + '/dns_records/' + CloudflareDynDnsRecordId,
                        method='PUT',
                        data=bytes(json.dumps(data), encoding='utf8'),
                        headers={
                            'Authorization': 'Bearer ' + config['cloudflare']['token'],
                            'Content-Type': 'application/json'
                        }
                    ))
                    logger.info('Updated ' + config['dyndns']['dyndns_target'] + ' to ' + data['content'])
                    oldExternalIPv4 = externalIPv4 # Will be retried if not successful
                except Exception as e:
                    logger.exception('Cloudflare A-record update error.')
                    sendTelegramNotification(f'Something went wrong at the Cloudflare A-record updater: {e}', False)
            
            externalIsPrimary = True in [externalIPv4 in n for n in primarySubnets]
            externalIsSecondary = True in [externalIPv4 in n for n in secondarySubnets]
            if externalIsPrimary or (not bothSubnetsSet and not externalIsSecondary):
                primaryConfidence += 1
            elif externalIsSecondary or (not bothSubnetsSet and not externalIsPrimary):
                primaryConfidence = 0
            else:
                logger.warning('External IP (' + str(externalIPv4) + ') is in neither the primary (' + str(primarySubnets) + ') nor the secondary (' + str(secondarySubnets) + ') subnet -> ignoring...')
            logger.debug('External IP is ' + str(externalIPv4))
        except Exception as e:
            logger.exception('External IPv4 resolve error.')
            primaryConfidence = 0
            sendTelegramNotification(f'Something went wrong at the external IPv4 resolver: {e}', False)

        # And update the dns entry of Cloudflare...
        def updateDynamicCname(config, data) -> bool:
            try:
                urlopen(Request(
                    'https://api.cloudflare.com/client/v4/zones/' + config['cloudflare']['zone_id'] + '/dns_records/' + CloudflareDnsRecordId,
                    method='PUT',
                    data=bytes(json.dumps(data), encoding='utf8'),
                    headers={
                        'Authorization': 'Bearer ' + config['cloudflare']['token'],
                        'Content-Type': 'application/json'
                    }
                ))
                logger.info('Updated ' + config['general']['dynamic_cname'] + ' to ' + data['content'])
                return True
            except Exception as e:
                logger.exception('Cloudflare CNAME-record update error.')
                sendTelegramNotification(f'Something went wrong at the Cloudflare CNAME updater: {e}', False)
                return False

        if primaryConfidence == config['primary']['confidence'] and not primaryActive:
            data = {
                'type': 'CNAME',
                'name': config['general']['dynamic_cname'],
                'content': config['primary']['cname'],
                'ttl': config['primary']['ttl'],
                'proxied': False
            }
            if updateDynamicCname(config, data):
                sendTelegramNotification(f'Primary network connection *STABLE* since `{primaryConfidence}` checks. Failover INACTIVE. Current IPv4 is `{externalIPv4}`.', True)
                primaryActive = True
        elif primaryConfidence == 0 and primaryActive:
            data = {
                'type': 'CNAME',
                'name': config['general']['dynamic_cname'],
                'content': config['secondary']['cname'],
                'ttl': config['secondary']['ttl'],
                'proxied': False
            }
            if updateDynamicCname(config, data):
                sendTelegramNotification(f'Primary network connection *FAILED*. Failover ACTIVE. Recheck in `{loopTime}` seconds... Current IPv4 is `{externalIPv4}`.', True)
                primaryActive = False
        logger.debug('primaryConfidence? ' + str(primaryConfidence))
        
        # Wait until next check...
        logger.debug('Sleeping...')
        HealthcheckEndpoint.lastLoop = datetime.datetime.now()
        time.sleep(loopTime)
except KeyboardInterrupt:
    pass
        
logger.info('Bye!')
healthcheckServer.shutdown() # stop the healthcheck server