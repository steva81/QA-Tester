from flask import Flask, render_template_string

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
def fail_page():
    # Return 500 error intentionally
    return "Internal Server Error", 500

@app.route('/js-error')
def js_error_page():
    return render_template_string(JS_ERROR_TEMPLATE)

if __name__ == '__main__':
    app.run(port=8080)
