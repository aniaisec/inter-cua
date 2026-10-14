"""A tiny real HTTP target for the shared Surface contract (no provider)."""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()


@app.get("/panel", response_class=HTMLResponse)
def panel(inject: str = ""):
    if inject == "error":
        return HTMLResponse("<h1>Service unavailable</h1>", status_code=500)
    if inject == "frames":
        return HTMLResponse(
            '<iframe name="nav" src="/nav"></iframe><iframe name="main" src="/panel"></iframe>'
        )
    duplicate = (
        "<fieldset><legend>Legacy</legend><h2>Legacy</h2><button>Calculate</button></fieldset>"
        if inject == "ambiguous"
        else ""
    )
    action = "alert('The rate service is unavailable.');return;" if inject == "modal" else ""
    return HTMLResponse(
        """<!doctype html><title>DeskCalc contract</title>
        <label>First number <input aria-label="First number" id="first"></label><br>
        <label>Second number <input aria-label="Second number" id="second"></label><br>
        <label>Operation <select aria-label="Operation" id="operation">
        <option>Add</option><option>Multiply</option></select></label><br>
        <button onclick="calculate()">Calculate</button><br>
        <label>Result <input aria-label="Result" id="result" readonly></label>
        """
        + duplicate
        + "<script>function calculate(){"
        + action
        + """
        const a=Number(document.getElementById('first').value);
        const b=Number(document.getElementById('second').value);
        document.getElementById('result').value=
          document.getElementById('operation').value==='Multiply' ? a*b : a+b;
        }</script>"""
    )


@app.get("/nav", response_class=HTMLResponse)
def nav():
    return HTMLResponse("<button>Calculate</button>")
