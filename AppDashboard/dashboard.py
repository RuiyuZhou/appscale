#!/usr/bin/env python
""" The AppDashboard is a Google App Engine application that implements a web UI
for interacting with running AppScale deployments. This includes the ability to
create new users, change their authorizations, and upload/remove Google App
Engine applications.
"""
# pylint: disable-msg=F0401
# pylint: disable-msg=C0103
# pylint: disable-msg=E1101
# pylint: disable-msg=W0613

import cgi
import datetime
import jinja2
import json
import logging
import os
import re
import sys
import time
import urllib
import webapp2

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext.db.stats import KindStat
from google.appengine.ext import ndb
from google.appengine.datastore.datastore_query import Cursor

sys.path.append(os.path.dirname(__file__) + '/lib')
from app_dashboard_helper import AppDashboardHelper
from app_dashboard_helper import AppHelperException
from app_dashboard_data import AppDashboardData
from app_dashboard_data import InstanceInfo
from app_dashboard_data import RequestInfo
from app_dashboard_data import AppStatus

from dashboard_logs import AppLogLine
from dashboard_logs import RequestLogLine

jinja_environment = jinja2.Environment(
  loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + \
                                 os.sep + 'templates'))

# The maximum number of datapoints we send to be rendered in a graph 
# charting requests per second.
MAX_REQUESTS_DATA_POINTS = 100


class LoggedService(ndb.Model):
  """ A Datastore Model that represents all of the machines running in this
  AppScale deployment.

  Fields:
    hosts: A list of strs, where each str corresponds to the hostname (an IP or
      a FQDN) of a machine running in this AppScale cloud.
  """
  hosts = ndb.StringProperty(repeated=True)


class AppDashboard(webapp2.RequestHandler):
  """ Class that all pages in the Dashboard must inherit from. """

  # Regular expression to capture the continue url.
  CONTINUE_URL_REGEX = 'continue=(.*)$'

  # Regular expression for updating user permissions.
  USER_PERMISSION_REGEX = '^user_permission_'

  # Regular expression that matches email addresses.
  USER_EMAIL_REGEX = '^\w[^@\s]*@[^@\s]{2,}$'

  # The frequency, in seconds, that defines how often Task Queue tasks are fired
  # to update the Dashboard's Datastore cache.
  REFRESH_WAIT_TIME = 10

  def __init__(self, request, response):
    """ Constructor.
    
    Args:
      request: The webapp2.Request object that contains information about the
        current web request.
      response: The webapp2.Response object that contains the response to be
        sent back to the browser.
    """
    self.initialize(request, response)
    self.helper = AppDashboardHelper()
    self.dstore = AppDashboardData(self.helper)

  def render_template(self, template_file, values=None):
    """ Renders a template file with all variables loaded.

    Args: 
      template_file: A str with the relative path to template file.
      values: A dict with key/value pairs used as variables in the jinja
        template files.
    Returns:
      A str with the rendered template.
    """
    if values is None:
      values = {}

    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.dstore.get_application_info()
    if not is_cloud_admin:
      apps_user_owns = self.helper.get_owned_apps()
      new_app_dict = {}
      for app_name in apps_user_owns:
        if app_name in apps_user_is_admin_on:
          new_app_dict[app_name] = apps_user_is_admin_on.get(app_name)
      apps_user_is_admin_on = new_app_dict

    self.helper.update_cookie_app_list(apps_user_is_admin_on.keys(),
                                       self.request, self.response)
    template = jinja_environment.get_template(template_file)
    sub_vars = {
      'logged_in': self.helper.is_user_logged_in(),
      'user_email': self.helper.get_user_email(),
      'is_user_cloud_admin': self.dstore.is_user_cloud_admin(),
      'can_upload_apps': self.dstore.can_upload_apps(),
      'apps_user_is_admin_on': apps_user_is_admin_on,
      'user_layout_pref': self.dstore.get_dash_layout_settings(),
      'flower_url': self.dstore.get_flower_url(),
      'monit_url': self.dstore.get_monit_url()
    }
    for key in values.keys():
      sub_vars[key] = values[key]
    return template.render(sub_vars)

  def get_shared_navigation(self, page):
    """ Renders the shared navigation.

    Returns:
      A str with the navigation bar rendered.
    """
    show_create_account = True
    if AppDashboardHelper.USE_SHIBBOLETH:
      show_create_account = False
    return self.render_template(template_file='shared/navigation.html',
                                values={'show_create_account':
                                        show_create_account,
                                        'page_name': page})

  def render_page(self, page, template_file, values=None):
    """ Renders a template with the main layout and nav bar. """
    if values is None:
      values = {}
    self.response.headers['Content-Type'] = 'text/html'
    template = jinja_environment.get_template('layouts/main.html')
    self.response.out.write(template.render(
      page_name=page,
      page_body=self.render_template(template_file, values),
      shared_navigation=self.get_shared_navigation(page)
    ))

  def render_app_page(self, page, values=None):
    self.render_page(page=page, template_file="layouts/app_page.html",
                     values=values)


class IndexPage(AppDashboard):
  """ Class to handle requests to the / page. """

  # The template to use for the index page.
  TEMPLATE = 'landing/index.html'

  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='landing', template_file=self.TEMPLATE, values={
      'monitoring_url': self.dstore.get_monitoring_url(),
    })


class DashPage(AppDashboard):
  """ Class to handle requests to the /status page. """

  # The path for the status page.
  PATH = '/'

  # Another url that serves the status page.
  ALIAS = '/status'

  # The template to use for the status page.
  TEMPLATE = 'apps/dash.html'

  def get(self):
    """ Handler for GET requests. """
    # Called from the web.  Refresh data then display page (may be slow).
    if self.request.get('forcerefresh'):
      self.dstore.update_all()

    self.render_page(page='dash', template_file=self.TEMPLATE, values={
      'server_info': self.dstore.get_status_info(),
      'dbinfo': self.dstore.get_database_info(),
      'apps': self.dstore.get_application_info().keys(),
      'monitoring_url': self.dstore.get_monitoring_url(),
    })


class DashRefreshPage(AppDashboard):
  """ Class to handle requests to the /status/refresh page. """

  def get(self):
    """ Handler for GET requests. Updates all the datastore values with
        information from the AppController and UserAppServer."""
    # Called from taskqueue. Refresh data and display status message.
    self.dstore.update_all()
    self.response.out.write('datastore updated')

  def post(self):
    """ Handler for POST requests. Updates all the datastore values with
        information from the AppController and UserAppServer."""
    # Called from taskqueue. Refresh data and display status message.
    self.dstore.update_all()
    self.response.out.write('datastore updated')


class StatusPage(AppDashboard):
  """ Class to handle requests to the /status page. """

  # The path for the status page.
  PATH = '/status/cloud'

  # Another url that serves the status page.
  ALIAS = '/status/cloud'

  # The template to use for the status page.
  TEMPLATE = 'status/cloud.html'

  def get(self):
    """ Handler for GET requests. """
    # Called from the web.  Refresh data then display page (may be slow).
    if self.request.get('forcerefresh'):
      self.dstore.update_all()

    self.render_app_page(page='status', values={
      'server_info': self.dstore.get_status_info(),
      'dbinfo': self.dstore.get_database_info(),
      'apps': self.dstore.get_application_info(),
      'monitoring_url': self.dstore.get_monitoring_url(),
      'page_content': self.TEMPLATE,
    })


class StatusAsJSONPage(webapp2.RequestHandler):
  """ A class that exposes the same information as DashPage, but via JSON
  instead of raw HTML. """

  def get(self):
    """ Retrieves the cached information about machine-level statistics as a
    JSON-encoded dict. """
    self.response.out.write(json.dumps(AppDashboardData().get_status_info()))


class NewUserPage(AppDashboard):
  """ Class to handle requests to the /users/new and /users/create page. """

  # The template to use for the new user page.
  TEMPLATE = 'users/new.html'

  # An int that indicates how many characters passwords must be for new user
  # accounts.
  MIN_PASSWORD_LENGTH = 6

  def parse_new_user_post(self):
    """ Parse the input from the create user form.

    Returns:
      A dict that maps the form fields on the user creation page to None (if
        they pass our validation) or a str indicating why they fail our
        validation.
    """
    users = {}
    error_msgs = {}
    users['email'] = cgi.escape(self.request.get('user_email'))
    if re.match(self.USER_EMAIL_REGEX, users['email']):
      error_msgs['email'] = None
    else:
      error_msgs['email'] = 'Format must be foo@boo.goo.'

    users['password'] = cgi.escape(self.request.get('user_password'))
    if len(users['password']) >= self.MIN_PASSWORD_LENGTH:
      error_msgs['password'] = None
    else:
      error_msgs['password'] = 'Password must be at least {0} characters ' \
                               'long.'.format(self.MIN_PASSWORD_LENGTH)

    users['password_confirmation'] = cgi.escape(
      self.request.get('user_password_confirmation'))
    if users['password_confirmation'] == users['password']:
      error_msgs['password_confirmation'] = None
    else:
      error_msgs['password_confirmation'] = 'Passwords do not match.'

    return error_msgs

  def process_new_user_post(self, errors):
    """ Creates new user if parse was successful.

    Args:
      errors: A dict with True/False values for errors in each of the users
              fields.
    Returns:
      True if user was created, and False otherwise.
    """
    if errors['email'] or errors['password'] or errors['password_confirmation']:
      return False
    else:
      return self.helper.create_new_user(cgi.escape(
        self.request.get('user_email')), cgi.escape(
        self.request.get('user_password')), self.response)

  def post(self):
    """ Handler for POST requests. """
    err_msgs = self.parse_new_user_post()
    try:
      if self.process_new_user_post(err_msgs):
        self.redirect(DashPage.PATH, self.response)
        return
    except AppHelperException as err:
      err_msgs['email'] = str(err)

    users = {}
    users['email'] = cgi.escape(self.request.get('user_email'))
    users['password'] = cgi.escape(self.request.get('user_password'))
    users['password_confirmation'] = cgi.escape(
      self.request.get('user_password_confirmation'))

    self.render_page(page='users', template_file=self.TEMPLATE, values={
      'user': users,
      'error_message_content': err_msgs,
    })

  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='users', template_file=self.TEMPLATE, values={
      'user': {},
      'error_message_content': {}
    })


class LoginVerify(AppDashboard):
  """ Class to handle requests to /users/confirm and /users/verify pages.

  This page is not currently used in the default login implementation, but the
  handler remains for compatibility with other implementations.
  """

  # The template to use for confirmation page.
  TEMPLATE = 'users/confirm.html'

  def post(self):
    """ Handler for POST requests. """
    if self.request.get('continue') != '' and \
            self.request.get('commit') == 'Yes':
      self.redirect(self.request.get('continue').encode('ascii', 'ignore'),
                    self.response)
    else:
      if AppDashboardHelper.USE_SHIBBOLETH:
        self.redirect(AppDashboardHelper.SHIBBOLETH_CONNECTOR, self.response)
      else:
        self.redirect(DashPage.PATH, self.response)

  def get(self):
    """ Handler for GET requests. """
    continue_url = urllib.unquote(self.request.get('continue'))
    url_match = re.search(self.CONTINUE_URL_REGEX, continue_url)
    if url_match:
      continue_url = url_match.group(1)

    self.render_page(page='users', template_file=self.TEMPLATE, values={
      'continue': continue_url
    })


class LogoutPage(AppDashboard):
  """ Class to handle requests to the /users/logout page. """

  def get(self):
    """ Handler for GET requests. Removes the AppScale login cookie and
        redirects the user to the landing page.
    """
    self.helper.logout_user(self.response)
    continue_url = self.request.get("continue")
    if continue_url:
      self.redirect(str(continue_url), self.response)
    else:
      if AppDashboardHelper.USE_SHIBBOLETH:
        self.redirect(AppDashboardHelper.SHIBBOLETH_CONNECTOR, self.response)
      else:
        self.redirect(DashPage.PATH, self.response)


class LoginPage(AppDashboard):
  """ Class to handle requests to the /users/login page. """

  # The path for the login page.
  PATH = '/login'

  # Another path that points to the login page.
  ALIAS = '/users/login'

  # Another path that points to the login page.
  ALIAS_2 = '/users/authenticate'

  # The template to use for rendering the login page.
  TEMPLATE = 'users/login.html'

  def post(self):
    """ Handler for POST requests. """
    user_email = self.request.get('user_email').lstrip().rstrip()
    if self.helper.login_user(user_email, self.request.get('user_password'),
                              self.response):

      if self.request.get('continue') != '':
        continue_url = self.request.get('continue').encode('ascii', 'ignore')
        self.redirect(continue_url, self.response)
      else:
        self.dstore.rebuild_dash_layout_settings_dict(email=user_email)
        self.redirect('/', self.response)
    else:
      flash_message = 'Incorrect username / password combination. ' \
                      'Please try again.'
      show_create_account = True
      if AppDashboardHelper.USE_SHIBBOLETH:
        show_create_account = False
      self.render_page(page='users', template_file=self.TEMPLATE,
                       values={
                         'continue': self.request.get('continue'),
                         'user_email': user_email,
                         'flash_message': flash_message,
                         'show_create_account': show_create_account
                       })

  def get(self):
    """ Handler for GET requests. """
    show_create_account = True
    if AppDashboardHelper.USE_SHIBBOLETH:
      show_create_account = False
    self.render_page(page='users', template_file=self.TEMPLATE, values={
      'continue': self.request.get('continue'),
      'show_create_account': show_create_account
    })


class ShibbolethLoginPage(AppDashboard):
  """ Class to handle requests to the Shibboleth login page. """

  # The path for the Shibboleth login page.
  PATH = '/login'

  # Another path that points to the login page.
  ALIAS = '/users/login'

  # Another path that points to the login page.
  ALIAS_2 = '/users/authenticate'

  def get(self):
    """ Handler for GET requests. """
    logging.info("LoginPage: continue -> {0}".format(
      self.request.get('continue')))
    user_email = self.request.get('HTTP_SHIB_INETORGPERSON_MAIL').strip(). \
      lower()
    logging.info("LoginPage: user_email: {0}".format(user_email))
    if user_email:
      self.redirect("{1}/users/shibboleth?continue={0}".format(
        self.request.get('continue'),
        AppDashboardHelper.SHIBBOLETH_CONNECTOR))

    target = '{0}/users/shibboleth?continue={1}'.format(
      AppDashboardHelper.SHIBBOLETH_CONNECTOR,
      self.request.get('continue'))
    self.redirect('{0}/Shibboleth.sso/Login?target={1}'.format(
      AppDashboardHelper.SHIBBOLETH_CONNECTOR,
      urllib.quote(target, safe='')))


class ShibbolethRedirect(AppDashboard):
  """ Class that handles the Shibboleth redirect. """

  # The path for the Shibboleth redirect.
  PATH = '/users/shibboleth'

  def get(self):
    """ Handler for GET requests. """
    user_email = os.environ.get('HTTP_SHIB_INETORGPERSON_MAIL').strip() \
      .lower()

    self.helper.create_token(user_email, user_email)
    user_app_list = self.helper.get_user_app_list(user_email)
    self.helper.set_appserver_cookie(user_email, user_app_list, self.response)

    if self.request.get('continue') != '':
      continue_url = self.request.get('continue').encode('ascii', 'ignore')
      self.redirect(continue_url, self.response)
    else:
      self.redirect(AppDashboardHelper.SHIBBOLETH_CONNECTOR, self.response)


class AuthorizePage(AppDashboard):
  """ Class to handle requests to the /authorize page. """

  # The template to use for the authorize page.
  TEMPLATE = 'authorize/cloud.html'

  def parse_update_user_permissions(self):
    """ Update authorization matrix from form submission.
    
    Returns:
      A str with message to be displayed to the user.
    """
    perms = self.helper.get_all_permission_items()
    req_keys = self.request.POST.keys()
    response = ''
    for fieldname, email in self.request.POST.iteritems():
      if re.match(self.USER_PERMISSION_REGEX, fieldname):
        for perm in perms:
          key = "{0}-{1}".format(email, perm)
          if key in req_keys and \
                  self.request.get('CURRENT-{0}'.format(key)) == 'False':
            if self.helper.add_user_permissions(email, perm):
              response += 'Enabling {0} for {1}. '.format(perm, email)
            else:
              response += 'Error enabling {0} for {1}. '.format(perm, email)
          elif key not in req_keys and \
                  self.request.get('CURRENT-{0}'.format(key)) == 'True':
            if self.helper.remove_user_permissions(email, perm):
              response += 'Disabling {0} for {1}. '.format(perm, email)
            else:
              response += 'Error disabling {0} for {1}. '.format(perm, email)
    return response

  def post(self):
    """ Handler for POST requests. """
    if self.dstore.is_user_cloud_admin():
      try:
        taskqueue.add(url='/status/refresh')
      except Exception as err:
        logging.exception(err)
      self.render_app_page(page='authorize', values={
        'flash_message': self.parse_update_user_permissions(),
        'user_perm_list': self.helper.list_all_users_permissions(),
        'page_content': self.TEMPLATE,
      })
    else:
      self.render_app_page(page='authorize', values={
        'flash_message': "Only the cloud administrator can change permissions.",
        'user_perm_list': {},
        'page_content': self.TEMPLATE,
      })

  def get(self):
    """ Handler for GET requests. """
    if self.dstore.is_user_cloud_admin():
      self.render_app_page(page='authorize', values={
        'user_perm_list': self.helper.list_all_users_permissions(),
        'page_content': self.TEMPLATE,
      })
    else:
      self.render_app_page(page='authorize', values={
        'flash_message': "Only the cloud administrator can change permissions.",
        'user_perm_list': {},
        'page_content': self.TEMPLATE,
      })


class ChangePasswordPage(AppDashboard):
  """Class to handle user password changes."""

  # The template to use for the change password page.
  TEMPLATE = 'authorize/cloud.html'

  def post(self):
    """ Handler for POST requests. """
    email = self.request.get("email")
    password = self.request.get("password")
    if self.dstore.is_user_cloud_admin():
      success, message = self.helper.change_password(cgi.escape(email),
                                                     cgi.escape(password))
    else:
      success = False
      message = "Only the cloud administrator can change passwords."

    flash_message = None
    error_flash_message = None
    if success:
      flash_message = message
    else:
      error_flash_message = message

    self.render_app_page(page='authorize', values={
      'flash_message': flash_message,
      'error_flash_message': error_flash_message,
      'user_perm_list': self.helper.list_all_users_permissions(),
      'page_content': self.TEMPLATE,
    })

  def get(self):
    """ Handler for GET requests. """
    if self.dstore.is_user_cloud_admin():
      self.render_app_page(page='authorize', values={
        'user_perm_list': self.helper.list_all_users_permissions(),
        'page_content': self.TEMPLATE,
      })
    else:
      self.render_app_page(page='authorize', values={
        'flash_message': "Only the cloud administrator can change permissions.",
        'user_perm_list': {},
        'page_content': self.TEMPLATE,
      })


class AppUploadPage(AppDashboard):
  """ Class to handle requests to the /apps/new page. """

  # The template to use for the upload app page.
  TEMPLATE = 'apps/new.html'

  def post(self):
    """ Handler for POST requests. """
    success_msg = ''
    err_msg = ''
    if not self.request.POST.multi or \
            'app_file_data' not in self.request.POST.multi or \
            not hasattr(self.request.POST.multi['app_file_data'], 'file'):
      self.render_app_page(page='apps', values={
        'error_message': 'You must specify a file to upload.',
        'success_message': '',
        'page_content': self.TEMPLATE,
      })
      return

    if self.dstore.can_upload_apps():
      try:
        success_msg = self.helper.upload_app(
          self.request.POST.multi['app_file_data'].filename,
          self.request.POST.multi['app_file_data'].file)
      except AppHelperException as err:
        self.response.set_status(500)
        err_msg = str(err)
      if success_msg:
        try:
          taskqueue.add(url='/status/refresh')
          taskqueue.add(url='/status/refresh', countdown=self.REFRESH_WAIT_TIME)
        except Exception as err:
          logging.exception(err)
    else:
      err_msg = "You are not authorized to upload apps."
    self.render_app_page(page='apps', values={
      'error_message': err_msg,
      'success_message': success_msg,
      'page_content': self.TEMPLATE,
    })

  def get(self):
    """ Handler for GET requests. """
    self.render_app_page(page='apps', values={
      'page_content': self.TEMPLATE,
    })


class AppDeletePage(AppDashboard):
  """ Class to handle requests to the /apps/delete page. """

  # The template to use for the app deletion page.
  TEMPLATE = 'apps/delete.html'

  def post(self):
    """ Handler for POST requests. """
    appname = self.request.POST.get('appname')
    if self.dstore.is_user_cloud_admin() or \
            appname in self.dstore.get_owned_apps():
      message = self.helper.delete_app(appname)
      self.dstore.delete_app_from_datastore(appname)
      try:
        taskqueue.add(url='/status/refresh')
        taskqueue.add(url='/status/refresh', countdown=self.REFRESH_WAIT_TIME)
      except Exception as err:
        logging.exception(err)
    else:
      message = "You do not have permission to delete the application: " \
                "{0}".format(appname)

    self.render_app_page(page='apps', values={
      'flash_message': message,
      'page_content': self.TEMPLATE,
    })

  def get(self):
    """ Handler for GET requests. """
    self.render_app_page(page='apps', values={
      'page_content': self.TEMPLATE,
    })


class AppRelocatePage(AppDashboard):
  """ Class to handle requests to the /apps/new page. """

  # The template to use for the upload app page.
  TEMPLATE = 'apps/relocate.html'

  def post(self):
    """ Handler for POST requests. """
    success_msg = ''
    err_msg = ''
    if not self.request.POST.multi or \
            'app_id' not in self.request.POST.multi:
      self.render_app_page(page='apps', values={
        'error_message': 'You must specify an app to relocate.',
        'success_message': '',
        'page_content': self.TEMPLATE,
      })
      return

    app_id = self.request.POST.get('app_id')
    if self.dstore.is_user_cloud_admin() or \
            app_id in self.dstore.get_owned_apps():
      try:
        success_msg = self.helper.relocate_app(
          self.request.POST.multi['app_id'],
          self.request.POST.multi['http_port'],
          self.request.POST.multi['https_port'])
      except AppHelperException as err:
        self.response.set_status(500)
        err_msg = str(err)
      if success_msg:
        try:
          taskqueue.add(url='/status/refresh')
          taskqueue.add(url='/status/refresh', countdown=self.REFRESH_WAIT_TIME)
        except Exception as err:
          logging.exception(err)
    else:
      err_msg = "You are not authorized to relocate that application."

    self.render_app_page(page='apps', values={
      'error_message': err_msg,
      'success_message': success_msg,
      'page_content': self.TEMPLATE,
    })

  def get(self):
    """ Handler for GET requests. """
    self.render_app_page(page='apps', values={
      'page_content': self.TEMPLATE,
    })


class AppsAsJSONPage(webapp2.RequestHandler):
  """ A class that exposes application-level info used on the Cloud Status page,
  but via JSON instead of raw HTML. """

  def get(self):
    """ Retrieves the cached information about applications running in this
    AppScale deployment as a JSON-encoded dict. """
    is_cloud_admin = AppDashboardHelper().is_user_cloud_admin()
    apps_user_is_admin_on = AppDashboardData().get_application_info()
    if not is_cloud_admin:
      apps_user_owns = AppDashboardHelper().get_owned_apps()
      new_app_dict = {}
      for app_name in apps_user_owns:
        if app_name in apps_user_is_admin_on:
          new_app_dict[app_name] = apps_user_is_admin_on.get(app_name)
      apps_user_is_admin_on = new_app_dict
    self.response.out.write(json.dumps(apps_user_is_admin_on))

  def post(self, app_id):
    """ Saves profiling information about a Google App Engine application to the
    Datastore, for viewing by the GET method.

    Args:
      app_id: A str that uniquely identifies the Google App Engine application
        we are storing data for.
    """
    encoded_data = self.request.body
    data = json.loads(encoded_data)

    the_time = int(data['timestamp'])
    reversed_time = (2 ** 34 - the_time) * 1000000
    request_info = RequestInfo(
      id=app_id + str(reversed_time),  # puts entities time descending.
      app_id=app_id,
      timestamp=datetime.datetime.fromtimestamp(data['timestamp']),
      num_of_requests=data['request_rate'])
    request_info.put()


class LogMainPage(AppDashboard):
  """ Class to handle requests to the /logs page. """

  # The template to use for the logs page.
  TEMPLATE = 'logs/main.html'

  def get(self):
    """ Handler for GET requests. """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (not apps_user_is_admin_on):
      self.redirect(DashPage.PATH, self.response)

    query = ndb.gql('SELECT * FROM LoggedService')
    all_services = []
    for entity in query:
      if entity.key.id() not in all_services:
        all_services.append(entity.key.id())

    permitted_services = []
    for service in all_services:
      if is_cloud_admin or service in apps_user_is_admin_on:
        permitted_services.append(service)

    self.render_app_page(page='logs', values={
      'services': permitted_services,
      'page_content': self.TEMPLATE,
    })


class LogServicePage(AppDashboard):
  """ Class to handle requests to the /logs/service_name page. """

  # The template to use for the logs service page.
  TEMPLATE = 'logs/service.html'

  def get(self, service_name):
    """ Displays a list of hosts that have logs for the given service. """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (service_name not in apps_user_is_admin_on):
      self.redirect(DashPage.PATH, self.response)

    service = LoggedService.get_by_id(service_name)
    if service:
      exists = True
      hosts = service.hosts
    else:
      exists = False
      hosts = []

    self.render_app_page(page='logs', values={
      'exists': exists,
      'service_name': service_name,
      'hosts': hosts
    })


class LogServiceHostPage(AppDashboard):
  """ Class to handle requests to the /logs/service_name/host page. """

  # The template to use for the logs viewer for the instance.
  TEMPLATE = 'logs/viewer.html'

  # The number of logs we should present on each page.
  LOGS_PER_PAGE = 10

  def get(self, service_name, host):
    """ Displays all logs accumulated for the given service, on the named host.

    Specifying 'all' as the host indicates that we shouldn't restrict ourselves
    to a single machine.
    """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (service_name not in apps_user_is_admin_on):
      self.redirect(DashPage.PATH, self.response)

    encoded_cursor = self.request.get('next_cursor')
    if encoded_cursor and encoded_cursor != "None":
      start_cursor = Cursor(urlsafe=encoded_cursor)
    else:
      start_cursor = None

    if host == "all":
      query, next_cursor, is_more = RequestLogLine.query(
        RequestLogLine.service_name == service_name).fetch_page(
        self.LOGS_PER_PAGE, produce_cursors=True, start_cursor=start_cursor)
    else:
      query, next_cursor, is_more = RequestLogLine.query(
        RequestLogLine.service_name == service_name,
        RequestLogLine.host == host).fetch_page(self.LOGS_PER_PAGE,
                                                produce_cursors=True,
                                                start_cursor=start_cursor)

    if next_cursor:
      cursor_value = next_cursor.urlsafe()
    else:
      cursor_value = None

    self.render_app_page(page='logs', values={
      'service_name': service_name,
      'host': host,
      'query': query,
      'next_cursor': cursor_value,
      'is_more': is_more,
      'page_content': self.TEMPLATE,
    })


class LogUploadPage(webapp2.RequestHandler):
  """ Class to handle requests to the /logs/upload page. """

  def post(self):
    """ Saves logs records to the Datastore for later viewing. """
    encoded_data = self.request.body
    data = json.loads(encoded_data)
    service_name = data['service_name']
    host = data['host']
    log_lines = data['logs']

    # First, check to see if this service has been registered.
    service = LoggedService.get_by_id(service_name)
    if service is None:
      service = LoggedService(id=service_name)
      service.hosts = [host]
      service.put()
    else:
      if host not in service.hosts:
        service.hosts.append(host)
        service.put()

    # Next, add in each log line as an AppLogLine
    entities_to_store = {}
    for log_line_dict in log_lines:
      the_time = int(log_line_dict['timestamp'])
      reversed_time = (2 ** 34 - the_time) * 1000000
      key_name = service_name + host + str(reversed_time)
      log_line = None
      # Check the local cache first.
      if key_name in entities_to_store:
        log_line = entities_to_store[key_name]
      else:
        # Grab it from the datastore. 
        log_line = RequestLogLine.get_by_id(id=key_name)
      if not log_line:
        # This is the first log for this timestamp.
        log_line = RequestLogLine(id=key_name)
        log_line.service_name = service_name
        log_line.host = host
        # Catch entity so that it does not repeatedly get fetched.
        entities_to_store[key_name] = log_line

      # Update the log entry with the given timestamp.
      app_log_line = AppLogLine()
      app_log_line.message = log_line_dict['message']
      app_log_line.level = log_line_dict['level']
      app_log_line.timestamp = datetime.datetime.fromtimestamp(the_time)

      # We append to the list property of the log line.
      log_line.app_logs.append(app_log_line)
      # Update our local cache with the new version of the log line.
      entities_to_store[key_name] = log_line

    batch_put = []
    for key_name in entities_to_store:
      batch_put.append(entities_to_store[key_name])
    ndb.put_multi(batch_put)


class LogDownloader(AppDashboard):
  """ Exposes a single GET route that cloud administrators can access to
  download AppScale-generated logs.
  """

  # The location where the template file can be found that waits for logs
  # to become available before redirecting to it.
  TEMPLATE = "logs/download.html"

  def get(self):
    """ Instructs the AppController to collect logs across all machines, place
    it in this app's static file directory, and renders a page that will wait
    for the logs to become available before downloading it.
    """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    if not is_cloud_admin:
      self.redirect(DashPage.PATH)

    success, uuid = self.helper.gather_logs()
    self.render_app_page(page='logs', values={
      'success': success,
      'uuid': uuid,
      'page_content': self.TEMPLATE,
    })


class AppConsolePage(AppDashboard):
  # The template to use for the app console page.
  TEMPLATE = "apps/console.html"

  def get(self):
    self.render_app_page(page='console', values={
      'page_content': self.TEMPLATE,
    })


class DatastoreStats(AppDashboard):
  """ Class that returns datastore statistics in JSON such as the number of 
  a certain entity kind and the amount of total bytes.
  """
  # The most number of data points we pass back to render in the dashboard.
  MAX_KIND_STATS = 1000

  # The most number of days we look back to get kind statistics.
  MAX_DAYS_BACK = 30

  def get(self):
    """ Handler for GET request for the datastore statistics. 

    Returns:
      The JSON output for testing.
    """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    app_name = self.request.get("appid")
    if (not is_cloud_admin) and (app_name not in apps_user_is_admin_on):
      response = json.dumps({"error": True, "message": "Not authorized"})
      self.response.out.write(response)
      return

    query = KindStat.all(_app=app_name)
    time_stamp = datetime.datetime.now() - datetime.timedelta(
      days=self.MAX_DAYS_BACK)
    query.filter("timestamp >", time_stamp)
    items = query.fetch(self.MAX_KIND_STATS)

    response = self.convert_to_json(items)
    self.response.out.write(response)
    return

  def convert_to_json(self, kind_entities):
    """ Converts KindStat entities to a json string.
  
    Args:
      kind_entities: A list of stats.KindStat.
    Returns:
      A JSON string containing kind statistic information.
    """
    items = []
    for ent in kind_entities:
      items.append({time.mktime(ent.timestamp.timetuple()):
                    {ent.kind_name: {'bytes': ent.bytes,
                                     "count": ent.count}}})
    return json.dumps(items)


class RequestsStats(AppDashboard):
  """ Class that returns request statistics in JSON relating to the number 
  of requests an application gets per second.
  """

  def get(self):
    """ Handler for GET request for the requests statistics. """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    app_name = self.request.get("appid")
    if (not is_cloud_admin) and (app_name not in apps_user_is_admin_on):
      response = json.dumps({"error": True, "message": "Not authorized"})
      self.response.out.write(response)
      return

    appid = self.request.get("appid")
    self.response.out.write(json.dumps(RequestsStats.fetch_request_info(appid)))

  @staticmethod
  def fetch_request_info(app_id):
    """ Fetches request per second information from the datastore for 
    a given application.
  
    Args:
      app_id: A str, the application identifier.
    Returns:
      A list of dictionaries filled with timestamps and number of 
      requests per second.
    """
    query = RequestInfo.query(RequestInfo.app_id == app_id)
    requests = query.fetch(MAX_REQUESTS_DATA_POINTS)
    request_info = []
    for request in requests:
      request_info.append({
        'timestamp': int(request.timestamp.strftime('%s')),
        'num_of_requests': request.num_of_requests
      })
    return request_info


class InstanceStats(AppDashboard):
  """ Class that returns instance statistics in JSON relating to the number
  of AppServer processes running for a particular App Engine application.
  """

  def get(self):
    """ Makes sure the user is allowed to see instance data for the named
    application, and if so, retrieves it for them. """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    app_name = self.request.get("appid")
    if (not is_cloud_admin) and (app_name not in apps_user_is_admin_on):
      response = json.dumps({"error": True, "message": "Not authorized"})
      self.response.out.write(response)
      return

    appid = self.request.get("appid")
    self.response.out.write(json.dumps(InstanceStats.fetch_request_info(appid)))

  def post(self):
    """ Adds information about one or more instances to the Datastore, for
    later viewing.
    """
    encoded_data = self.request.body
    data = json.loads(encoded_data)

    for instance in data:
      # TODO: Consider only doing a put if it doesn't exist
      instance = InstanceInfo(id=instance['appid'] + instance['host'] + \
                              str(instance['port']),
                              appid=instance['appid'],
                              host=instance['host'],
                              port=instance['port'],
                              language=instance['language'])
      instance.put()

    self.response.out.write('put completed successfully!')

  def delete(self):
    """ Removes information about one or more instances from the Datastore. """
    encoded_data = self.request.body
    data = json.loads(encoded_data)

    for instance in data:
      instance = InstanceInfo.get_by_id(instance['appid'] + instance['host'] + \
                                        str(instance['port']))
      instance.key.delete()

    self.response.out.write('delete completed successfully!')

  @staticmethod
  def fetch_request_info(appid):
    """ Retrieves information about the AppServer processes running the
    application associated with the named application.

    Args:
      appid: A str, the application identifier.
    Returns:
      A list of dicts, where each dict has information about a single AppServer
      process running the named application.
    """
    query = InstanceInfo.query(InstanceInfo.appid == appid)
    instances = query.fetch()
    return [{
              'host': instance.host,
              'port': instance.port,
              'language': instance.language
            } for instance in instances]


class MemcacheStats(AppDashboard):
  """ Class that returns global memcache statistics. """

  def get(self):
    """ Handler for GET request for the memcache statistics. """
    if not self.helper.is_user_cloud_admin():
      response = json.dumps({"error": True, "message": "Not authorized"})
      self.response.out.write(response)
      return

    mem_stats = memcache.get_stats()
    self.response.out.write(json.dumps(mem_stats))


class StatsPage(AppDashboard):
  """ Class to handle requests to the /apps/stats page. """

  # The template to use for the stats page.
  TEMPLATE = 'apps/stats.html'

  def get(self):
    # Only let the cloud admin and users who own this app see this page.
    app_id = self.request.get('appid')
    is_cloud_admin = self.helper.is_user_cloud_admin()

    if is_cloud_admin:
      apps_user_is_admin_on = self.dstore.get_application_info().keys()
    else:
      apps_user_is_admin_on = self.helper.get_owned_apps()

    if not apps_user_is_admin_on:
      self.redirect(DashPage.PATH, self.response)

    if app_id not in apps_user_is_admin_on:
      self.redirect(DashPage.PATH, self.response)

    instance_info = InstanceStats.fetch_request_info(app_id)

    app_status = AppStatus.get_by_id(app_id)
    if app_status and app_status.url:
      url = app_status.url
    else:
      url = None

    self.render_app_page(page='stats', values={
      'appid': app_id,
      'all_apps_this_user_owns': apps_user_is_admin_on,
      'instance_info': instance_info,
      'app_url': url,
      'page_content': self.TEMPLATE,
    })


class RunGroomer(AppDashboard):
  """ Class that dynamically updates Kind statistics in the Datastore. """

  def get(self):
    """ Calls the groomer and tells it that Kind statistics need to be
    updated. """
    self.response.out.write(json.dumps({
      'result': self.helper.run_groomer()
    }))


class AjaxRenderPanel(AppDashboard):
  """ Class that adds panels to the dashboard. """

  def get(self):
    """ Calls render_template to return the correct panel """
    key_val = self.request.get('key_val')
    self.response.out.write(self.render_template(
      template_file='layouts/panel.html',
      values={'page_info': self.dstore.get_panel_key_info(key_val),
              'id': key_val}))


class AjaxSaveLayoutSettings(AppDashboard):
  """ Class that stores dashboard layout settings in the Datastore. """

  def post(self):
    """ sets the dashboard layout settings """

    nav = self.request.get("nav")
    panel = self.request.get("panel")
    saved_dict = {"nav": json.loads(nav), "panel": json.loads(panel)}
    try:
      self.dstore.set_dash_layout_settings(values=saved_dict)
      self.response.set_status(200)
      self.response.out.write("Saved")
    except Exception as err:
      logging.exception(err)
      self.response.set_status(500)
      self.response.out.write("Try Again")


class AjaxResetLayoutSettings(AppDashboard):
  """ Class that stores dashboard layout settings in the Datastore. """

  def post(self):
    """ sets the dashboard layout settings """
    try:
      self.dstore.set_dash_layout_settings()
      self.response.set_status(200)
      self.response.out.write("Layout Reset")
    except Exception as err:
      logging.exception(err)
      self.response.set_status(500)
      self.response.out.write("Try Again")


# Main Dispatcher
dashboard_pages = [
  (DashPage.PATH, DashPage),
  (DashPage.ALIAS, DashPage),
  ('/status/refresh', DashRefreshPage),
  ('/status/cloud', StatusPage),
  ('/status/json', StatusAsJSONPage),
  ('/logout', LogoutPage),
  ('/users/logout', LogoutPage),
  ('/users/verify', LoginVerify),
  ('/users/confirm', LoginVerify),
  ('/authorize', AuthorizePage),
  ('/apps/?', AppConsolePage),
  ('/apps/stats/datastore', DatastoreStats),
  ('/apps/stats/requests', RequestsStats),
  ('/apps/stats/instances', InstanceStats),
  ('/apps/stats/memcache', MemcacheStats),
  ('/apps/new', AppUploadPage),
  ('/apps/upload', AppUploadPage),
  ('/apps/relocate', AppRelocatePage),
  ('/apps/delete', AppDeletePage),
  ('/apps/json/?', AppsAsJSONPage),
  ('/apps/json/(.+)', AppsAsJSONPage),
  ('/apps/stats', StatsPage),
  ('/logs', LogMainPage),
  ('/logs/upload', LogUploadPage),
  ('/logs/(.+)/(.+)', LogServiceHostPage),
  ('/logs/(.+)', LogServicePage),
  ('/gather-logs', LogDownloader),
  ('/groomer', RunGroomer),
  ('/change-password', ChangePasswordPage),
  ('/ajax/panel/render', AjaxRenderPanel),
  ('/ajax/layout/save', AjaxSaveLayoutSettings),
  ('/ajax/layout/reset', AjaxResetLayoutSettings)
]

if AppDashboardHelper.USE_SHIBBOLETH:
  dashboard_pages.extend([
    (ShibbolethLoginPage.PATH, ShibbolethLoginPage),
    (ShibbolethLoginPage.ALIAS, ShibbolethLoginPage),
    (ShibbolethLoginPage.ALIAS_2, ShibbolethLoginPage),
    (ShibbolethRedirect.PATH, ShibbolethRedirect)
  ])
else:
  dashboard_pages.extend([
    (LoginPage.PATH, LoginPage),
    (LoginPage.ALIAS, LoginPage),
    (LoginPage.ALIAS_2, LoginPage),
    ('/users/new', NewUserPage),
    ('/users/create', NewUserPage)
  ])

app = webapp2.WSGIApplication(dashboard_pages, debug=True)


def handle_404(_, response, exception):
  """ Handles 404, page not found exceptions. """
  logging.exception(exception)
  response.set_status(404)
  response.write(jinja_environment.get_template('404.html').render())


def handle_500(_, response, exception):
  """ Handles 500, error processing page exceptions. """
  logging.exception(exception)
  response.set_status(500)
  response.write(jinja_environment.get_template('500.html').render())


app.error_handlers[404] = handle_404
app.error_handlers[500] = handle_500
