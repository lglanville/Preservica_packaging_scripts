import time
import pathlib
import sys
import requests
import datetime
import json
from threading import Thread
from io import BytesIO
from lxml import etree
import logging
import argparse
import subprocess
FORMAT = '%(asctime)-15s [%(levelname)s] %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger('siplog')
logger.setLevel(logging.INFO)
ENT_MAP = {
    "information-objects": "InformationObject",
    "structural-objects": "StructuralObject",
    "content-objects": "ContentObject"}


def find_config():
    """find a config.json file, looking in a preservica folder
    in your home directory, then the location of the calling script."""
    configpath = pathlib.Path().home() / '.preservica/config.json'
    if not configpath.exists():
        configpath = pathlib.Path(sys.argv[0]).parent / 'config.ini'
        if not configpath.exists():
            print("No config file found")
            exit()
    with configpath.open() as f:
        config = json.load(f)
    return config


def write_config(host, tenant, login, password, profile='DEFAULT'):
    """Write or amend a config.json file with the credentials provided"""
    configpath = pathlib.Path().home() / '.preservica/config.json'
    if configpath.exists():
        with configpath.open() as f:
            config = json.load(f)
    else:
        if not configpath.parent.exists():
            configpath.parent.mkdir()
        config = {}
    config[profile] = {
        'Host': host, 'Tenant': tenant,
        'Username': login, 'Password': password}
    with configpath.open('w') as f:
        json.dump(config, f, indent=1)


def get_session(profile='DEFAULT'):
    """Create a preservica session using a config file."""
    config = find_config()
    host = config[profile]['Host']
    username = config[profile]['Username']
    password = config[profile]['Password']
    tenant = config[profile]['Tenant']
    sesh = preservica_session(username, password, host, tenant)
    return sesh


class preservica_session(requests.Session):
    """Class that handles authentication and wraps useful requests to the
    Preservica REST API. Best used as a context manager."""

    def __init__(self, login, password, host, tenant):
        super(preservica_session, self).__init__()
        logging.info("Staring session")
        self.host = host
        self.tenant = tenant
        self.headers = {
                    'Accept': "*/*",
                    'Content-Type': 'application/xml',
                    'Cache-Control': "no-cache",
                    'Host': self.host,
                    'Accept-Encoding': "gzip, deflate",
                    'Content-Length': "0",
                    'Connection': "keep-alive",
                    'cache-control': "no-cache"
                    }
        self.baseurl = "https://"+self.host
        self.entityurl = self.baseurl+"/api/entity"
        self.authenturl = self.baseurl+"/api/accesstoken"
        self.get_token(login, password)

    def close(self):
        """
        Revokes the current token and cancels the refresh timer on session
        close. Make sure the session is closed explicitly, or use as context
        manager,otherwise update timer will cause session to hang indefinitely
        """
        url = self.authenturl+"/revoke"
        self.post(
            url,
            params={"access-token": self.headers['Preservica-Access-Token']})
        super(preservica_session, self).close()

    def get_token(self, login, password):
        """
        Gets an access token from Preservica and appends it to the session
        headers. Starts a timer to refresh after 10 minutes.
        """
        url = self.authenturl+"/login"
        querystring = {
            "username": login, "password": password, "tenant": self.tenant}
        logging.info("Authenticating with Preservica")
        response = self.post(url, params=querystring)
        if response.status_code == 200:
            data = response.json()
            tokenval = (data["token"])
            self.headers['Preservica-Access-Token'] = tokenval
            self.refresh_token = data["refresh-token"]
            self.refresh_timer = Thread(target=self.refresh, daemon=True)
            self.refresh_timer.start()
        else:
            logging.error(f"Unable to authenticate, received status code {response.status_code}")

    def refresh(self, interval=600):
        """
        Refreshes access token after a given interval and restarts
        refresh timer.
        """
        time.sleep(interval)
        url = self.authenturl+"/refresh"
        logging.info("Refreshing authentication token")
        response = self.post(url, params={"refreshToken": self.refresh_token})
        data = response.json()
        tokenval = (data["token"])
        self.headers['Preservica-Access-Token'] = tokenval
        self.refresh_token = data["refresh-token"]
        self.refresh_timer = Thread(target=self.refresh, daemon=True)
        self.refresh_timer.start()

    def get_refs(self, identifier, type='code'):
        """
        Returns a dict of entity types with lists of references matching the
        provided identifier.
        """
        typemap = {
            "IO": "information-objects",
            "SO": "structural-objects",
            "CO": "content-objects"}
        parameters = {'type': type, 'value': identifier}
        r = self.get(
            self.entityurl+"/entities/by-identifier",
            params=parameters)
        tree = etree.parse(BytesIO(r.content))
        root = tree.getroot()
        refs = {
            "structural-objects": [], "information-objects": [],
            "content-objects": []}
        for ent in root.findall('.//Entity', namespaces=root.nsmap):
            entitytype = typemap[ent.attrib['type']]
            refs[entitytype].append(ent.attrib['ref'])
        return refs

    def get_object(self, ref, type):
        url = "https://unimelb.preservica.com/api/entity/"+type+"/"+ref
        r = self.get(url)
        if r.status_code == 200:
            tree = etree.parse(BytesIO(r.content))
            root = tree.getroot()
            return root

    def get_metadata(self, ref, type):
        object = self.get_object(ref, type)
        metadata = []
        for frag in object.findall('.//Metadata/Fragment', namespaces=object.nsmap):
            metadata.append({'schema': frag.get('schema'), 'uri': frag.text})
        return metadata

    def post_metadata(self, ref, type, fragment):
        url = self.entityurl+"/"+type+"/"+ref+"/metadata"
        r = self.post(url, data=fragment)
        if r.status_code == 200:
            logging.info(f'Successfully added metadata fragment to {ref}')
        else:
            logging.error(f'Problem adding metadata to {ref}')

    def replace_metadata(self, metaurl, fragment):
        self.headers['Content-Type'] = 'application/xml'
        r = self.put(metaurl, data=fragment)
        if r.status_code == 200:
            logging.info(f'Successfully replaced metadata fragment {metaurl}')
        else:
            logging.error(f'Error replacing metadata fragment {metaurl}, status code {r.status_code}')

    def update_xipmeta(self, ref, type, tag, text):
        entity = self.get_object(ref, type)
        xip = entity.find('xip:'+ENT_MAP[type], namespaces=entity.nsmap)
        xip.find('xip:'+tag, namespaces=entity.nsmap).text = text
        data = etree.tostring(xip, pretty_print=True).decode()
        url = entity.findtext(
            'AdditionalInformation/Self', namespaces=entity.nsmap)
        self.put(url, data=data)

    def update_extended_xip(self, ref, type, earliest, latest, surrogate=True):
        nspace = "http://preservica.com/ExtendedXIP/v6.0"
        extended_xip = etree.Element('ExtendedXIP', nsmap={None: nspace})
        etree.SubElement(
            extended_xip, 'DigitalSurrogate').text = str(surrogate).lower()
        etree.SubElement(
            extended_xip, 'CoverageFrom').text = earliest
        etree.SubElement(
            extended_xip, 'CoverageTo').text = latest
        extended_xip = etree.tostring(extended_xip, pretty_print=True).decode()
        meta = self.get_metadata(ref, type)
        xip_frags = [m for m in meta if m['schema'] == nspace]
        if xip_frags != []:
            meta_uri = xip_frags[0]['uri']  # we're assuming there's only one
            self.replace_metadata(meta_uri, extended_xip)
        else:
            self.post_metadata(ref, type, extended_xip)

    def upload(self, fpath, target):
        """Uploads package to the target folder. Note if a parent is specified
        in the package XIP it will override the provided target.
        """
        self.headers['Content-Type'] = "application/octet-stream"
        fpath = pathlib.Path(fpath)
        url = self.entityurl
        +'/structural-objects/'+target+"/upload-package?filename=" + fpath.name
        start_time = time.time()
        logging.info(f"Upload of {fpath} commencing")
        try:
            with fpath.open('rb') as data:
                response_mref = self.post(url, data=data)
                duration = time.time() - start_time
                if response_mref.status_code == 200:
                    logging.info(
                        f"Upload of {fpath} complete,"
                        f" duration {duration}")
                else:
                    logging.error(
                        f"Upload of {fpath} failed with status"
                        f" {response_mref.status_code}")
                return(response_mref.text)
        except OSError as e:
            print(e)
        self.headers['Content-Type'] = 'application/xml'

    def s3upload(self, fpath, bucket):
        """uploadspackage to S3 biucket with required metadata. Needs the
        AWS CLI installed and configured. We might do this via Boto in the
        future"""
        p = pathlib.Path(fpath)
        size = round(p.stat().st_size/1024)
        args = [
            'aws', 's3', 'cp', fpath, f's3://{bucket}', '--metadata',
            f'key={p.name},name={p.name+".zip"},size={size}']
        r = subprocess.run(args, check=True, stdout=subprocess.PIPE)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Simple tasks using the Preservica API')
    parser.add_argument(
        '--config', nargs=4,
        metavar=('host', 'tenant', 'username', 'password'),
        help='saves or amends a credentials file')
    parser.add_argument(
        '--upload', nargs=2, metavar=('filepath', 'parentref'),
        help='uploads a package to parent ref')
    args = parser.parse_args()
    if args.config is not None:
        write_config(*args.config)
    sesh = get_session()
    if args.upload is not None:
        sesh.upload(*args.upload)
