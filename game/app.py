import datetime
import os
import pprint
import uuid
from tempfile import mkdtemp
from flask import Flask, jsonify, request, render_template, url_for
from flask_caching import Cache
from werkzeug.exceptions import Forbidden
from pylti1p3.contrib.flask import FlaskOIDCLogin, FlaskMessageLaunch, FlaskRequest
from pylti1p3.deep_link_resource import DeepLinkResource
from pylti1p3.grade import Grade
from pylti1p3.lineitem import LineItem
from pylti1p3.tool_config import ToolConfJsonFile


class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        scheme = environ.get('HTTP_X_FORWARDED_PROTO')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)


app = Flask('pylti1p3-game-example', template_folder='templates', static_folder='static')
app.wsgi_app = ReverseProxied(app.wsgi_app)

config = {
    "DEBUG": True,
    "ENV": "development",
    "CACHE_TYPE": "simple",
    "CACHE_DEFAULT_TIMEOUT": 600,
    "SECRET_KEY": "replace-me",
    "SESSION_TYPE": "filesystem",
    "SESSION_FILE_DIR": mkdtemp(),
    "SESSION_COOKIE_NAME": "flask-session-id",
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SECURE": False,  # should be True in case of HTTPS usage (production)
    "SESSION_COOKIE_SAMESITE": None,  # should be 'None' in case of HTTPS usage (production)
    "APPEND_TIMEZONE" : False # Must be set to true if using Blackboard Learn
}
app.config.from_mapping(config)
cache = Cache(app)

PAGE_TITLE = 'Game Example'


class ExtendedFlaskMessageLaunch(FlaskMessageLaunch):

    def validate_nonce(self):
        """
        Probably it is bug on "https://lti-ri.imsglobal.org":
        site passes invalid "nonce" value during deep links launch.
        Because of this in case of iss == http://imsglobal.org just skip nonce validation.

        """
        iss = self._get_iss()
        deep_link_launch = self.is_deep_link_launch()
        if iss == "http://imsglobal.org" and deep_link_launch:
            return self
        return super(ExtendedFlaskMessageLaunch, self).validate_nonce()


def get_lti_config_path():
    return os.path.join(app.root_path, '..', 'configs', 'game.json')


@app.route('/check-cookies-allowed/', methods=['GET'])
def check_cookies_allowed():
    test_cookie_val = request.cookies.get('test_cookie', None)
    request_ts = request.args.get('ts', None)
    cookie_sent = bool(request_ts and test_cookie_val and request_ts == test_cookie_val)
    return jsonify({'cookies_allowed': cookie_sent})


@app.route('/login/', methods=['GET', 'POST'])
def login():
    cookies_allowed = str(request.args.get('cookies_allowed', ''))

    # check cookies and ask to open page in the new window in case if cookies are not allowed
    # https://chromestatus.com/feature/5088147346030592
    # to share GET/POST data between requests we save them into cache
    if cookies_allowed:
        login_unique_id = str(request.args.get('login_unique_id', ''))
        if not login_unique_id:
            raise Exception('Missing "login_unique_id" param')

        login_data = cache.get(login_unique_id)
        if not login_data:
            raise Exception("Can't restore login data from cache")

        tool_conf = ToolConfJsonFile(get_lti_config_path())
        request_params_dict = {}
        request_params_dict.update(login_data['GET'])
        request_params_dict.update(login_data['POST'])

        oidc_request = FlaskRequest(request_data=request_params_dict)
        oidc_login = FlaskOIDCLogin(oidc_request, tool_conf)
        target_link_uri = request_params_dict.get('target_link_uri')
        return oidc_login.redirect(target_link_uri)
    else:
        login_unique_id = str(uuid.uuid4())
        cache.set(login_unique_id, {
            'GET': request.args.to_dict(),
            'POST': request.form.to_dict()
        }, 3600)
        tpl_kwargs = {
            'login_unique_id': login_unique_id,
            'same_site': app.config['SESSION_COOKIE_SAMESITE'],
            'site_protocol': 'https' if request.is_secure else 'http',
            'page_title': PAGE_TITLE
        }
        return render_template('check_cookie.html', **tpl_kwargs)


@app.route('/launch/', methods=['GET', 'POST'])
def launch():
    launch_unique_id = str(request.args.get('launch_id', ''))

    # reload page in case if session cookie is unavailable (chrome samesite issue):
    # https://chromestatus.com/feature/5088147346030592
    # to share GET/POST data between requests we save them into cache
    session_key = request.cookies.get(app.config['SESSION_COOKIE_NAME'], None)
    if not session_key and not launch_unique_id:
        launch_unique_id = str(uuid.uuid4())
        cache.set(launch_unique_id, {
            'GET': request.args.to_dict(),
            'POST': request.form.to_dict()
        }, 3600)
        current_url = request.base_url
        if '?' in current_url:
            current_url += '&'
        else:
            current_url += '?'
        current_url = current_url + 'launch_id=' + launch_unique_id
        return '<script type="text/javascript">window.location="%s";</script>' % current_url

    launch_request = FlaskRequest()
    if request.method == "GET":
        launch_data = cache.get(launch_unique_id)
        if not launch_data:
            raise Exception("Can't restore launch data from cache")
        request_params_dict = {}
        request_params_dict.update(launch_data['GET'])
        request_params_dict.update(launch_data['POST'])
        launch_request = FlaskRequest(request_data=request_params_dict)

    tool_conf = ToolConfJsonFile(get_lti_config_path())
    message_launch = ExtendedFlaskMessageLaunch(launch_request, tool_conf)
    message_launch_data = message_launch.get_launch_data()
    pprint.pprint(message_launch_data)

    tpl_kwargs = {
        'page_title': PAGE_TITLE,
        'is_deep_link_launch': message_launch.is_deep_link_launch(),
        'launch_data': message_launch.get_launch_data(),
        'launch_id': message_launch.get_launch_id(),
        'curr_user_name': message_launch_data.get('name', ''),
        'curr_diff': message_launch_data.get('https://purl.imsglobal.org/spec/lti/claim/custom', {})
            .get('difficulty', 'normal')
    }
    return render_template('game.html', **tpl_kwargs)


@app.route('/configure/<launch_id>/<difficulty>/', methods=['GET', 'POST'])
def configure(launch_id, difficulty):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    flask_request = FlaskRequest()
    message_launch = ExtendedFlaskMessageLaunch.from_cache(launch_id, flask_request, tool_conf)

    if not message_launch.is_deep_link_launch():
        raise Forbidden('Must be a deep link!')

    launch_url = url_for('launch', _external=True)

    resource = DeepLinkResource()
    resource.set_url(launch_url) \
        .set_custom_params({'difficulty': difficulty}) \
        .set_title('Breakout ' + difficulty + ' mode!')

    html = message_launch.get_deep_link().output_response_form([resource])
    return html


@app.route('/api/score/<launch_id>/<earned_score>/<time_spent>/', methods=['POST'])
def score(launch_id, earned_score, time_spent):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    flask_request = FlaskRequest()
    message_launch = ExtendedFlaskMessageLaunch.from_cache(launch_id, flask_request, tool_conf)

    if not message_launch.has_ags():
        raise Forbidden("Don't have grades!")

    sub = message_launch.get_launch_data().get('sub')
    timestamp = datetime.datetime.utcnow().isoformat()
    if app.config['APPEND_TIMEZONE']:
        timestamp += 'Z'
    earned_score = int(earned_score)
    time_spent = int(time_spent)

    grades = message_launch.get_ags()
    sc = Grade()
    sc.set_score_given(earned_score) \
        .set_score_maximum(100) \
        .set_timestamp(timestamp) \
        .set_activity_progress('Completed') \
        .set_grading_progress('FullyGraded') \
        .set_user_id(sub)

    sc_line_item = LineItem()
    sc_line_item.set_tag('score') \
        .set_score_maximum(100) \
        .set_label('Score')

    grades.put_grade(sc, sc_line_item)

    tm = Grade()
    tm.set_score_given(time_spent) \
        .set_score_maximum(999) \
        .set_timestamp(timestamp) \
        .set_activity_progress('Completed') \
        .set_grading_progress('FullyGraded') \
        .set_user_id(sub)

    tm_line_item = LineItem()
    tm_line_item.set_tag('time') \
        .set_score_maximum(999) \
        .set_label('Time Taken')

    result = grades.put_grade(tm, tm_line_item)

    return jsonify({'success': True, 'result': result.get('body')})


@app.route('/api/scoreboard/<launch_id>/', methods=['GET', 'POST'])
def scoreboard(launch_id):
    tool_conf = ToolConfJsonFile(get_lti_config_path())
    flask_request = FlaskRequest()
    message_launch = ExtendedFlaskMessageLaunch.from_cache(launch_id, flask_request, tool_conf)

    if not message_launch.has_nrps():
        raise Forbidden("Don't have names and roles!")

    if not message_launch.has_ags():
        raise Forbidden("Don't have grades!")

    ags = message_launch.get_ags()

    score_line_item = LineItem()
    score_line_item.set_tag('score') \
        .set_score_maximum(100) \
        .set_label('Score')
    scores = ags.get_grades(score_line_item)

    time_line_item = LineItem()
    time_line_item.set_tag('time') \
        .set_score_maximum(999) \
        .set_label('Time Taken')
    times = ags.get_grades(time_line_item)

    members = message_launch.get_nrps().get_members()
    scoreboard_result = []

    for sc in scores:
        result = {'score': sc['resultScore']}
        for tm in times:
            if tm['userId'] == sc['userId']:
                result['time'] = tm['resultScore']
                break
        for member in members:
            if member['user_id'] == sc['userId']:
                result['name'] = member.get('name', 'Unknown')
                break
        scoreboard_result.append(result)

    return jsonify(scoreboard_result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9017)
