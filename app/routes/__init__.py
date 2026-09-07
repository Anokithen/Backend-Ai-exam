def register_blueprints(app):
    from app.routes.admin_routes import bp as admin_bp
    from app.routes.auth_routes import bp as auth_bp
    from app.routes.dashboard_routes import bp as dashboard_bp
    from app.routes.exam_routes import bp as exam_bp
    from app.routes.material_routes import bp as material_bp
    from app.routes.room_routes import bp as room_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(material_bp)
    app.register_blueprint(exam_bp)
    app.register_blueprint(room_bp)
