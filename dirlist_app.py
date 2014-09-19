import os
from flask import Blueprint
from flask.ext.autoindex import AutoIndexBlueprint

dirlist = Blueprint('dirlist_app', __name__)
AutoIndexBlueprint(dirlist, os.path.join(os.getcwd(), 'results'))
