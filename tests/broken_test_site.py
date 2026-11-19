from flask import Flask, render_template_string, redirect, request

app = Flask(__name__)

@app.route('/')
def home():
    return """
    <h1>Home</h1>
    <ul>
        <li><a href="/good">Good</a></li>
        <li><a href="/server-error">Server Error</a></li>
        <li><a href="/missing">Missing</a></li>
        <li><a href="/js-error">JS Error</a></li>
        <li><a href="/broken-image">Broken Image</a></li>
        <li><a href="/broken-script">Broken Script</a></li>
        <li><a href="/overflow">Overflow</a></li>
        <li><a href="/redirect">Redirect</a></li>
        <li><a href="/auth-protected">Auth Protected</a></li>
        <li><a href="/duplicate-id">Duplicate ID</a></li>
        <li><a href="/no-title">No Title</a></li>
        <li><a href="/missing-label">Missing Label</a></li>
    </ul>
    """

@app.route('/good')
def good():
    return "<!DOCTYPE html><html><head><title>Good Page</title></head><body><h1>Good</h1><p>Working.</p></body></html>"

@app.route('/server-error')
def server_error():
    return "Server Error", 500

@app.route('/missing')
def missing():
    return "Not Found", 404

@app.route('/js-error')
def js_error():
    return "<!DOCTYPE html><html><head><title>JS Error Page</title></head><body><script>throw new Error('Test exception');</script></body></html>"

@app.route('/broken-image')
def broken_image():
    return "<!DOCTYPE html><html><head><title>Broken Image</title></head><body><img src='/nonexistent.png' alt='missing'></body></html>"

@app.route('/broken-script')
def broken_script():
    return "<!DOCTYPE html><html><head><title>Broken Script</title><script src='/missing.js'></script></head><body></body></html>"

@app.route('/overflow')
def overflow():
    return "<!DOCTYPE html><html><head><title>Overflow</title><style>div { width: 5000px; height: 100px; background: red; }</style></head><body><div></div></body></html>"

@app.route('/redirect')
def redirect_page():
    return redirect('/good')

@app.route('/auth-protected')
def auth_protected():
    return redirect('/signin?redirect=/auth-protected')

@app.route('/signin')
def signin():
    return "<!DOCTYPE html><html><head><title>Sign In</title></head><body><form><label for='u'>User</label><input id='u' name='u'></form></body></html>"

@app.route('/duplicate-id')
def duplicate_id():
    return "<!DOCTYPE html><html><head><title>Duplicate ID</title></head><body><div id='test'></div><div id='test'></div></body></html>"

@app.route('/no-title')
def no_title():
    return "<!DOCTYPE html><html><head></head><body><h1>No Title</h1></body></html>"

@app.route('/missing-label')
def missing_label():
    return "<!DOCTYPE html><html><head><title>Missing Label</title></head><body><form><input type='text' name='q'></form></body></html>"

if __name__ == '__main__':
    app.run(port=8080)
