def assess_finding(title, actual_result, area, viewport):
    """
    Deterministically assesses a finding and returns a structured dictionary:
    {
        'severity': 'High' | 'Medium' | 'Low' | 'Unreviewed',
        'risk': 'High' | 'Medium' | 'Low' | 'Unreviewed',
        'confidence': 'High' | 'Medium' | 'Low' | 'Unreviewed',
        'impact': str,
        'recommendation': str
    }
    """
    actual = actual_result.lower() if actual_result else ''
    title_lower = title.lower() if title else ''
    
    # F. Page failed to return a status (must check this before generic status checks if we want to be safe, but it has specific strings)
    if 'failed to get response status' in actual or 'page failed to return a status' in title_lower:
        return {
            'severity': 'High',
            'risk': 'Medium',
            'confidence': 'Medium',
            'impact': "The page did not return a usable HTTP status during the QA test. If the same behavior occurs for users, they may be unable to access the affected page or service.",
            'recommendation': "Confirm that the target address is correct and that the service is reachable. Review available browser, network, server, or application logs to determine why a response status was not obtained, then retest."
        }
        
    # A. HTTP 500 / 5xx
    if '500' in actual or '502' in actual or '503' in actual or '504' in actual:
        return {
            'severity': 'High',
            'risk': 'High',
            'confidence': 'High',
            'impact': "The server returned an error response during the QA test. If the same behavior occurs for users, the affected page or function may be unavailable.",
            'recommendation': "Review the affected request and relevant server/application logs to determine why the error response occurred, then retest the affected workflow."
        }
        
    # B. HTTP 404
    if '404' in actual:
        return {
            'severity': 'Medium',
            'risk': 'Medium',
            'confidence': 'High',
            'impact': "The requested resource returned HTTP 404 during the QA test. If users encounter the same response, they may be unable to reach the intended page or complete the affected navigation.",
            'recommendation': "Verify that the target URL and application route are correct. If the resource has moved or was removed intentionally, update the affected navigation or provide an appropriate redirect where applicable, then retest."
        }
        
    # D. Navigation Failure / ERR_CONNECTION_REFUSED / Timeout
    if 'net::err_connection_refused' in actual or 'navigation failed' in actual:
        return {
            'severity': 'High',
            'risk': 'Medium',
            'confidence': 'Medium',
            'impact': "The target page could not be accessed during the QA test. If the same behavior occurs for real users, they may be unable to use the affected page or service.",
            'recommendation': "Confirm that the target website or test server is running and that the URL is correct. If the issue persists after those checks, investigate the server or network configuration."
        }
        
    # C. JavaScript Exception
    if 'javascript exception' in actual or 'console errors detected' in actual or 'uncaught javascript exception' in title_lower:
        return {
            'severity': 'Medium',
            'risk': 'Medium',
            'confidence': 'High',
            'impact': "A JavaScript exception was detected in the browser. Depending on where the error occurred, some page functionality may fail or behave unexpectedly.",
            'recommendation': "Review the recorded JavaScript error and available console or stack information to identify the affected code or dependency, then verify the behavior again after correction."
        }

    # I. Dangling aria-controls / Missing Target Panel
    if 'dangling_aria_controls' in actual or 'dangling target' in title_lower or 'element was not found in the dom' in actual:
        return {
            'severity': 'Medium',
            'risk': 'Medium',
            'confidence': 'High',
            'impact': "An interactive disclosure control references a non-existent DOM element via aria-controls. Screen reader users and assistive technologies receive broken element references, and the associated content cannot be located or controlled.",
            'recommendation': "Verify the aria-controls attribute on the control. Ensure the referenced ID exists in the DOM or update the markup so that aria-controls correctly identifies the target panel."
        }

    # J. Unrevealed Content / Content Not Revealed
    if 'content_not_revealed' in actual or 'unrevealed content' in title_lower or 'remained hidden' in actual:
        return {
            'severity': 'Medium',
            'risk': 'Medium',
            'confidence': 'High',
            'impact': "An expandable control toggled its aria-expanded state, but the associated content panel remained hidden. Users activating the control receive no visible content or feedback.",
            'recommendation': "Review the event handler and CSS rules associated with the control. Ensure that toggling the control properly removes the hidden attribute or updates display styles on the target panel."
        }

    # K. Control Click Script Error / Crash On Click
    if 'page_script_error' in actual or 'crash on click' in title_lower or 'during click, leaving control inert' in actual:
        return {
            'severity': 'High',
            'risk': 'High',
            'confidence': 'High',
            'impact': "Activating the control caused an uncaught JavaScript exception in the browser. The intended state transition was blocked and subsequent interactive page functionality may become unresponsive.",
            'recommendation': "Inspect the click event listener and stack trace. Address the uncaught exception (e.g. TypeError or null reference) to ensure that the control executes safely."
        }
        
    # E. Horizontal Overflow
    if 'horizontal overflow' in actual or 'horizontal scrolling' in actual:
        return {
            'severity': 'Low',
            'risk': 'Low',
            'confidence': 'High',
            'impact': "Content extended beyond the tested viewport width, causing horizontal scrolling. This can make the page harder to use, particularly on smaller screens.",
            'recommendation': "Review the affected layout at the recorded viewport size and identify which element exceeds the available width. Adjust the relevant responsive layout or content styling and retest."
        }
        
    # G. Missing Form Label
    if 'missing form label' in actual or 'missing form label' in title_lower:
        return {
            'severity': 'Unreviewed',
            'risk': 'Unreviewed',
            'confidence': 'Unreviewed',
            'impact': "An input field on the page lacks an associated label or accessibility attribute. Users relying on screen readers may not be able to identify the purpose of the input, and clicking label text will not focus the control. Because the automated check does not record the specific input element, human inspection is needed to locate the control and verify user impact.",
            'recommendation': "Inspect the page forms and identify any unlabeled input controls. Associate each control with a descriptive <label for=\"id\"> or provide an aria-label / aria-labelledby attribute, then verify accessibility manually."
        }

    # H. Missing Document Title
    if 'missing or empty document title' in actual or 'missing document title' in actual or 'missing document title' in title_lower:
        return {
            'severity': 'Unreviewed',
            'risk': 'Unreviewed',
            'confidence': 'Unreviewed',
            'impact': "The webpage is missing a <title> element in its <head> section, or the title element is blank. Browser tabs, window switchers, bookmarks, and search results may display the raw URL instead of an informative title, making tab navigation difficult.",
            'recommendation': "Add a descriptive, non-empty <title> element within the <head> section of the HTML document (e.g. <title>Page Name - Site Name</title>) and retest."
        }

    # Fallback
    return {
        'severity': 'Unreviewed',
        'risk': 'Unreviewed',
        'confidence': 'Unreviewed',
        'impact': "[Requires Human Review]",
        'recommendation': "[Requires Human Review]"
    }

