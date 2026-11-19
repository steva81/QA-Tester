from flask import Flask, render_template_string, redirect, request

app = Flask(__name__)

# A simple home page with links to other pages (testing crawling & dedup)
HOME_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Demo Home</title></head>
<body>
    <h1>Demo Home (PASS)</h1>
    <a href="/pass">Go to Pass Page</a><br>
    <a href="/fail">Go to Broken Page</a><br>
    <a href="/js-error">Go to JS Error Page</a><br>
    <a href="/pass#fragment">Go to Pass Page (Fragment)</a><br> <!-- Dedup test -->
    <a href="/broken-image">Broken Image</a><br>
    <a href="/broken-script">Broken Script</a><br>
    <a href="/overflow">Overflow</a><br>
    <a href="/redirect">Redirect</a><br>
    <a href="/auth-protected">Auth Protected</a><br>
    <a href="/duplicate-id">Duplicate ID</a><br>
    <a href="/no-title">No Title</a><br>
    <a href="/missing-label">Missing Label</a><br>
    <a href="/missing">Missing 404</a><br>
</body>
</html>
"""

PASS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Pass Page</title></head>
<body>
    <h1>Pass Page</h1>
    <p>Everything is fine here.</p>
    <a href="/">Back Home</a>
</body>
</html>
"""

JS_ERROR_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>JS Error Page</title></head>
<body>
    <h1>JS Error Page</h1>
    <script>
        console.error("This is an intentional console error for testing.");
        throw new Error("Uncaught test error");
    </script>
    <a href="/">Back Home</a>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HOME_TEMPLATE)

@app.route('/pass')
def pass_page():
    return render_template_string(PASS_TEMPLATE)

@app.route('/fail')
@app.route('/server-error')
def fail_page():
    # Return 500 error intentionally
    return "Internal Server Error", 500

@app.route('/js-error')
def js_error_page():
    return render_template_string(JS_ERROR_TEMPLATE)

@app.route('/missing')
def missing():
    return "Not Found", 404

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
    return redirect('/pass')

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
