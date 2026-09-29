from flask import Blueprint

staff_management_bp = Blueprint('staff_management', __name__, 
                                  template_folder='templates',
                                  static_folder='static')

from . import routes