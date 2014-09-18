#!/usr/bin/env python
# coding=utf8

import json
import os
import re
import threading
import time
from unicodedata import normalize

import docker
from flask import Flask, render_template, session, g, redirect, url_for, request, send_from_directory
from flask.ext.bootstrap import Bootstrap
from flask.ext.wtf import Form, FileField, validators
from wtforms.fields import TextAreaField
from werkzeug import secure_filename

import psutil
import requests
import tempfile

from flask.ext.openid import OpenID
import sqlite3dbm

app = Flask(__name__)
app.secret_key = "arglebargle"
oid = OpenID(app, os.path.join(os.path.dirname(__file__), 'openid_store'))

UPLOAD_FOLDER = tempfile.mkdtemp()
MOUNTPOINT = '/blah'
ALLOWED_EXTENSIONS = set(['py'])
DATABASE = '/usr/local/etc/jiffylab/webapp/database.sqlite3'

app.config['BOOTSTRAP_USE_MINIFIED'] = True
app.config['BOOTSTRAP_USE_CDN'] = True
app.config['BOOTSTRAP_FONTAWESOME'] = True
app.config['SECRET_KEY'] = app.secret_key
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

CONTAINER_STORAGE = "/usr/local/etc/jiffylab/webapp/containers.json"
SERVICES_HOST = '127.0.0.1'
BASE_IMAGE = 'ytproject/yt-devel'
BASE_IMAGE_TAG = 'latest'

initial_memory_budget = psutil.virtual_memory().free  # or can use available for vm

# how much memory should each container be limited to, in k.
CONTAINER_MEM_LIMIT = 1024 * 1024 * 128
# how much memory must remain in order for a new container to start?
MEM_MIN = CONTAINER_MEM_LIMIT + 1024 * 1024 * 128

app.config.from_object(__name__)
app.config.from_envvar('FLASKAPP_SETTINGS', silent=True)

Bootstrap(app)

docker_client = docker.Client(base_url='unix://var/run/docker.sock',
                  version='1.6',
                  timeout=10)

lock = threading.Lock()



def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1] in ALLOWED_EXTENSIONS
        


class ContainerException(Exception):
    """
    There was some problem generating or launching a docker container
    for the user
    """
    pass


_punct_re = re.compile(r'[\t !"#$%&\'()*\-/<=>?@\[\\\]^_`{|},.]+')


def slugify(text, delim=u'-'):
    """Generates a slightly worse ASCII-only slug."""
    result = []
    for word in _punct_re.split(text.lower()):
        word = normalize('NFKD', word).encode('ascii', 'ignore')
        if word:
            result.append(word)
    return unicode(delim.join(result))


def get_image(image_name=BASE_IMAGE, image_tag=BASE_IMAGE_TAG):
    # TODO catch ConnectionError - requests.exceptions.ConnectionError
    for image in docker_client.images():
        if image['Repository'] == image_name and image['Tag'] == image_tag:
            return image
    raise ContainerException("No image found")
    return None


def lookup_container(name):
    # TODO should this be reset at startup?
    container_store = app.config['CONTAINER_STORAGE']
    if not os.path.exists(container_store):
        with lock:
            json.dump({}, open(container_store, 'wb'))
        return None
    containers = json.load(open(container_store, 'rb'))
    try:
        return containers[name]
    except KeyError:
        return None


def check_memory():
    """
    Check that we have enough memory "budget" to use for this container

    Note this is hard because while each container may not be using its full
    emory limit amount, you have to consider it like a check written to your
    account, you never know when it may be cashed.
    """
    # the overbook factor says that each container is unlikely to be using its
    # full memory limit, and so this is a guestimate of how much you can overbook
    # your memory
    overbook_factor = .8
    remaining_budget = initial_memory_budget - len(docker_client.containers()) * CONTAINER_MEM_LIMIT * overbook_factor
    if remaining_budget < MEM_MIN:
        raise ContainerException("Sorry, not enough free memory to start your container")



def remember_container(name, containerid):
    container_store = app.config['CONTAINER_STORAGE']
    with lock:
        if not os.path.exists(container_store):
            containers = {}
        else:
            containers = json.load(open(container_store, 'rb'))
        containers[name] = containerid
        json.dump(containers, open(container_store, 'wb'))


def forget_container(name):
    container_store = app.config['CONTAINER_STORAGE']
    with lock:
        if not os.path.exists(container_store):
            return False
        else:
            containers = json.load(open(container_store, 'rb'))
        try:
            del(containers[name])
            json.dump(containers, open(container_store, 'wb'))
        except KeyError:
            return False
        return True

def add_portmap(cont):
    if cont['Ports']:
        # a bit of a crazy comprehension to turn:
        # Ports': u'49166->8888, 49167->22'
        # into a useful dict {8888: 49166, 22: 49167}
        cont['portmap'] = dict([(p['PrivatePort'], p['PublicPort']) for p in cont['Ports']])

        # wait until services are up before returning container
        # TODO this could probably be factored better when next
        # service added
        # this should be done via ajax in the browser
        # this will loop and kill the server if it stalls on docker
        ipy_wait = shellinabox_wait = True
        while ipy_wait or shellinabox_wait:
            if ipy_wait:
                try:
                    requests.head("http://{host}:{port}".format(
                            host=app.config['SERVICES_HOST'],
                            port=cont['portmap'][8888]))
                    ipy_wait = False
                except requests.exceptions.ConnectionError:
                    pass

            if shellinabox_wait:
                try:
                    requests.head("http://{host}:{port}".format(
                            host=app.config['SERVICES_HOST'],
                            port=cont['portmap'][4200]))
                    shellinabox_wait = False
                except requests.exceptions.ConnectionError:
                    pass
            time.sleep(.2)
            print 'waiting', app.config['SERVICES_HOST']
        return cont


def get_container(cont_id, all=False):
    # TODO catch ConnectionError
    for cont in docker_client.containers(all=all):
        if cont_id in cont['Id']:
            return cont
    return None


def get_or_make_container(email, filename):
    # TODO catch ConnectionError
    name = slugify(unicode(email)).lower()
    container_id = lookup_container(name)
    if not container_id:
        image = get_image()
        cont = docker_client.create_container(
                image['Id'],
                ['python', os.path.join(MOUNTPOINT, filename)],
                hostname="{user}box".format(user=name.split('-')[0]),
                mem_limit=CONTAINER_MEM_LIMIT,
                ports=[8888, 4200],
                volumes=[MOUNTPOINT]
                )

        remember_container(name, cont['Id'])
        container_id = cont['Id']

    container = get_container(container_id, all=True)

    if not container:
        # we may have had the container cleared out
        forget_container(name)
        print 'recurse'
        # recurse
        # TODO DANGER- could have a over-recursion guard?
        return get_or_make_container(email, filename)

    if "Up" not in container['Status']:
        # if the container is not currently running, restart it
        check_memory()
        docker_client.start(container_id,
                            binds={UPLOAD_FOLDER: {"bind": MOUNTPOINT}},
                            publish_all_ports=True)
        # refresh status
        container = get_container(container_id)
    #container = add_portmap(container)
    return name, container


@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        file = request.files['file']
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            return redirect(url_for('index', filename=filename))
    return '''
    <!doctype html>
    <title>Upload new File</title>
    <h1>Upload new File</h1>
    <form action="" method=post enctype=multipart/form-data>
      <p><input type=file name=file>
         <input type=submit value=Upload>
    </form>
    '''

@app.route('/main', methods=['GET', 'POST'])
def index():
    try:
        container = None
        print g.user
        if g.user:
            # return "hi user %(id)d (email %(email)s). <a href='/logout'>logout</a>" %(g.user)
            name, container = get_or_make_container(g.user, request.values['filename'])
            docker_client.wait(container.get('Id'))
            forget_container(name)
            return "%s" % docker_client.logs(container.get('Id'))
        return render_template('index.html',
                container=container,
                filename=request.values['filename'],
                servicehost=app.config['SERVICES_HOST'],
                )
    except ContainerException as e:
        session.pop('openid', None)
        return render_template('error.html', error=e)
 

def open_db():
    g.db = getattr(g, 'db', None) or sqlite3dbm.sshelve.open(DATABASE)
 
def get_user():
    open_db()
    return g.db.get('oid-' + session.get('openid', ''))
 
@app.before_request
def set_user_if_logged_in():
    open_db() # just to be explicit ...
    g.user = get_user()
 
@app.route("/login")
@oid.loginhandler
def login():
    if g.user is not None:
        return redirect(oid.get_next_url())
    else:
        return oid.try_login("https://www.google.com/accounts/o8/id",
            ask_for=['email', 'fullname', 'nickname'])
 
@oid.after_login
def new_user(resp):
    session['openid'] = resp.identity_url
    if get_user() is None:
        user_id = g.db.get('user-count', 0)
        g.db['user-count'] = user_id + 1
        g.db['oid-' + session['openid']] = {
            'id': user_id,
            'email': resp.email,
            'fullname': resp.fullname,
            'nickname': resp.nickname}
    return redirect(oid.get_next_url())
 
@app.route('/logout')
def logout():
    session.pop('openid', None)
    return redirect(oid.get_next_url())
 

if '__main__' == __name__:
    oid = OpenID(app, '/tmp/openid_store', safe_roots=[])
    # app.run(debug=True, host='0.0.0.0')
    pass


