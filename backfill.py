import sqlite3
from app.models.database import get_db_connection
from app.qa.explanations import get_explanations

def backfill_explanations():
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT id, description, plain_language_explanation, learning_explanation_sv, how_to_explain_sv FROM findings")
    findings = c.fetchall()
    
    updated_count = 0
    for finding in findings:
        f_id = finding['id']
        actual_result = finding['description']
        
        # Only overwrite if they are NULL or literal 'None' string due to jinja artifacts in DB
        pl_en = finding['plain_language_explanation']
        lrn_sv = finding['learning_explanation_sv']
        how_sv = finding['how_to_explain_sv']
        
        if not pl_en or pl_en == 'None' or not lrn_sv or lrn_sv == 'None' or not how_sv or how_sv == 'None':
            expl_en, expl_sv_learn, expl_sv_verb = get_explanations(actual_result=actual_result)
            
            # Keep existing value if it was manually edited (i.e. not None)
            new_pl = pl_en if (pl_en and pl_en != 'None') else expl_en
            new_lrn = lrn_sv if (lrn_sv and lrn_sv != 'None') else expl_sv_learn
            new_how = how_sv if (how_sv and how_sv != 'None') else expl_sv_verb
            
            c.execute('''
                UPDATE findings 
                SET plain_language_explanation = ?, learning_explanation_sv = ?, how_to_explain_sv = ?
                WHERE id = ?
            ''', (new_pl, new_lrn, new_how, f_id))
            updated_count += 1
            
    conn.commit()
    conn.close()
    print(f"Backfilled {updated_count} findings successfully.")

if __name__ == '__main__':
    backfill_explanations()
