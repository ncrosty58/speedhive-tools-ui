from flask import render_template, request, session, redirect, url_for


def login():
    from app import UI_PASSWORD
    error = None
    if request.method == "POST":
        if not UI_PASSWORD:
            error = "Site password is not configured (set SPEEDHIVE_UI_PASSWORD)."
        elif request.form.get("password", "") == UI_PASSWORD:
            session["authenticated"] = True
            next_path = request.form.get("next") or ""
            # only allow same-site relative redirects
            if next_path.startswith("/") and not next_path.startswith("//"):
                return redirect(next_path)
            return redirect(url_for("index"))
        else:
            error = "Incorrect password."
    if session.get("authenticated"):
        return redirect(url_for("index"))
    return render_template("login.html", error=error, next_path=request.args.get("next", ""))


def logout():
    session.clear()
    return redirect(url_for("login"))


def register_routes(app):
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout, methods=["POST"])
