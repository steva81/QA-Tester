def get_explanations(status_code=None, actual_result="", area=""):
    # Default fallbacks
    expl_en = "Further investigation is required to explain this finding."
    expl_sv_learn = "Det behövs mer information för att förklara detta fynd säkert."
    expl_sv_verbal = "Vi har hittat ett problem men det behövs mer analys för att förklara exakt vad det är."

    actual = str(actual_result).lower()
    
    if "http 500" in actual or (status_code and status_code >= 500):
        expl_en = "An HTTP 500 error means that the browser successfully reached the website, but the server encountered an internal problem while processing the request. As a result, the page could not be displayed normally. The QA test confirms that the server returned HTTP 500, but it does not determine the exact technical cause of the problem."
        expl_sv_learn = "Ett HTTP 500-fel betyder att webbplatsens server fick ett problem när den försökte hantera sidan. Webbläsaren nådde alltså servern, men servern lyckades inte slutföra begäran. QA-testet visar att sidan inte fungerar som användaren förväntar sig, men testet visar inte exakt vad som är fel inne på servern."
        expl_sv_verbal = "Jag hittade en sida som svarar med HTTP 500. Det betyder att servern får ett internt fel och därför inte kan visa sidan korrekt."
        
    elif "http 404" in actual or (status_code == 404):
        expl_en = "An HTTP 404 error means that the browser communicated with the server, but the server could not find the requested page. The QA test confirms the page is missing, but it does not prove if the link was incorrectly formatted or if the page was deleted."
        expl_sv_learn = "Ett HTTP 404-fel betyder att webbläsaren nådde servern, men servern kunde inte hitta den begärda sidan. Sidan saknas helt enkelt. QA-testet bekräftar att länken eller URL:en leder till en obefintlig sida, men inte varför den försvann."
        expl_sv_verbal = "Jag hittade en trasig länk som ger HTTP 404. Sidan som länken pekar på verkar inte finnas."
        
    elif "navigation failed" in actual:
        expl_en = "This may be caused by a server, network, or address-related issue. The QA test confirms that the page could not be reached, but it does not determine the underlying cause."
        expl_sv_learn = "Ett navigeringsfel innebär att webbläsaren inte kunde ansluta till webbplatsen överhuvudtaget. Det kan bero på att servern är nere eller att domännamnet inte fungerar. Testet visar att sidan inte går att ladda, men vi vet inte exakt var felet ligger i nätverket."
        expl_sv_verbal = "Webbläsaren kunde inte ansluta till adressen. Det kan bero på nätverksproblem, men vi kan inte säkert säga att servern är nere eller att adressen är fel."

    elif "blank page" in actual or "failed to get response status" in actual:
        expl_en = "A blank page or missing status suggests the application loaded without standard HTTP error codes but failed to render visible content. The QA test verifies that a normal user would see nothing, but it does not prove if the cause is a rendering bug or a blocked resource."
        expl_sv_learn = "En tom sida innebär att servern svarade, men webbläsaren kunde inte rita upp något innehåll. Det kan bero på att ett JavaScript kraschade innan något syntes, eller att servern skickade ett tomt dokument. Testet visar att användaren möts av en blank skärm."
        expl_sv_verbal = "Sidan laddar, men den är helt blank. Något hindrar innehållet från att visas."
        
    elif "console errors detected" in actual:
        expl_en = "A JavaScript exception was detected in the browser console. The QA test confirms an error was logged, but it does not prove the exact root cause or that the frontend logic definitely crashed."
        expl_sv_learn = "Ett JavaScript-fel i konsolen betyder att koden som körs i användarens webbläsare stötte på ett oväntat fel och kraschade delvis. Testet fångade upp detta fel, vilket kan innebära att vissa knappar eller funktioner på sidan slutar fungera, även om sidan ser normal ut."
        expl_sv_verbal = "Det uppstod ett JavaScript-fel i bakgrunden på sidan, vilket kan göra att interaktiva funktioner går sönder."

    elif "horizontal overflow" in actual:
        expl_en = "Horizontal overflow means that the webpage content is wider than the screen size, forcing the user to scroll sideways to see everything. The QA test proves that the responsive design broke on this viewport, but it does not identify which specific element is too wide."
        expl_sv_learn = "Ett horisontellt överflöde innebär att sidans innehåll är bredare än skärmen. På en mobiltelefon leder detta till att användaren måste scrolla i sidled, vilket ofta är ett tecken på trasig responsiv design. Testet visar att designen spricker på en liten skärm, men inte vilket specifikt element (t.ex. en bild eller tabell) som är för stort."
        expl_sv_verbal = "Sidan fungerar inte bra på mobila skärmar eftersom den blir för bred och användaren måste scrolla i sidled."

    elif "broken internal link" in actual:
        expl_en = "A broken internal link means a link pointing to another page on the same website does not work and leads to an error page. The QA test confirms the link is dead, but it does not explain whether the link itself is mistyped or the destination page was removed."
        expl_sv_learn = "En trasig intern länk innebär att en länk som pekar till en annan del av samma webbplats inte fungerar och leder till en felsida (ofta 404). Testet bekräftar att besökare kommer att fastna här, men vi vet inte om felet ligger i hur länken är skriven eller om målsidan har tagits bort."
        expl_sv_verbal = "Jag hittade en länk på sidan som leder till en felsida. Länken är död och behöver antagligen uppdateras."

    elif "unsafe request prevented" in actual or "blocked by qa tester" in actual:
        expl_en = "QA Tester actively prevented a potentially unsafe request (like a POST or DELETE) from executing on the live site to avoid accidental data changes. The QA test blocked it to be safe."
        expl_sv_learn = "QA Tester stoppade aktivt en potentiellt farlig förfrågan (som en POST eller DELETE) från att köras mot den riktiga sidan för att undvika oavsiktliga ändringar av data. Testet blockerade detta för säkerhets skull."
        expl_sv_verbal = "QA Tester blockerade en förfrågan som kunde ha ändrat data på webbplatsen."

    elif "err_failed" in actual:
        expl_en = "The browser could not complete a network request. In this case QA Tester had just blocked an unsafe POST request, so this is probably a consequence of QA Tester's safety protection rather than a separate website bug."
        expl_sv_learn = "Webbläsaren kunde inte slutföra en nätverksförfrågan. Om detta hände strax efter att QA Tester blockerade en förfrågan, så beror felet troligtvis på blockeringen och inte på en bugg i webbplatsen."
        expl_sv_verbal = "En nätverksförfrågan misslyckades. Om QA Tester just blockerade en förfrågan kan detta fel bero på säkerhetsspärren."

    elif "missing form label" in actual:
        expl_en = "A form input field on this page does not have an associated text label or accessibility description. Screen reader users might not know what to type into the field, and clicking a label won't focus it. The QA test detected an input without a label, but manual review is required to locate the exact field and confirm its context."
        expl_sv_learn = "Ett inmatningsfält på sidan saknar en tillhörande textetikett (<label>) eller tillgänglighetsbeskrivning. För användare med skärmläsare blir det svårt att förstå vad fältet ska användas till. Testet upptäckte att ett fält saknar etikett, men manuell granskning behövs för att hitta fältet och avgöra hur användarna påverkas."
        expl_sv_verbal = "Jag hittade ett inmatningsfält på sidan som saknar en tydlig etikett. Det gör sidan svårare att använda för personer med skärmläsare, och vi behöver undersöka vilket fält det gäller."

    elif "missing or empty document title" in actual or "missing document title" in actual:
        expl_en = "The webpage is missing a <title> tag in its <head> section, or the title tag is empty. Without a document title, the browser tab, bookmarks, and search results cannot display a descriptive name for the page. The QA test confirmed the <title> is absent or blank, but human review is recommended to choose an appropriate page title."
        expl_sv_learn = "Webbsidan saknar en <title>-tagg i <head>-sektionen, eller så är titeln helt tom. Utan en dokumenttitel visar webbläsarfliken, bokmärken och sökmotorer inget begripligt namn för sidan. QA-testet bekräftar att titeln saknas eller är tom, men en mänsklig granskare bör välja en lämplig och beskrivande titel."
        expl_sv_verbal = "Sidan saknar en sidtitel i webbläsaren. Det gör att fliken i webbläsaren inte visar vad sidan handlar om."

    elif "dangling_aria_controls" in actual or "element was not found in the dom" in actual or "nonexistent-target-element" in actual:
        expl_en = "An interactive disclosure control references a non-existent element via aria-controls. Screen reader users and assistive technologies receive broken accessibility references, and the associated content panel cannot be found."
        expl_sv_learn = "En interaktiv expanderbar kontroll refererar till ett element som inte finns i sidans DOM (via aria-controls). Detta gör att skärmläsare och hjälpmedel tappar kopplingen till innehållet som ska styras."
        expl_sv_verbal = "Knappen refererar till ett innehållselement som saknas i koden. Det gör att det inte går att hitta eller styra innehållet."

    elif "content_not_revealed" in actual or "remained hidden" in actual or "panel-unrevealed" in actual:
        expl_en = "An expandable control updated its state to expanded, but the target content remained hidden. Users who activate the control see no visible content or feedback."
        expl_sv_learn = "En knapp indikerar att innehållet har expanderats, men själva innehållspanelen förblir dold. Användare som klickar på knappen ser inget nytt innehåll."
        expl_sv_verbal = "Knappen ändrar sitt tillstånd till expanderad, men innehållet visas inte utan fortsätter att vara dolt."

    elif "page_script_error" in actual or "during click, leaving control inert" in actual or "typeerror" in actual:
        expl_en = "An uncaught JavaScript exception occurred when activating the control, leaving it inert and preventing the intended state change."
        expl_sv_learn = "Ett oväntat JavaScript-fel uppstod när knappen klickades, vilket kraschade skriptet och hindrade innehållet från att öppnas."
        expl_sv_verbal = "När knappen klickades uppstod ett JavaScript-fel som gjorde att knappen slutade fungera."

    return expl_en, expl_sv_learn, expl_sv_verbal


