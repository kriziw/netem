"""Native NetEm configuration for the traffic execution agent."""
import hmac

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for


def install(app, netem):
    sections = {'network', 'experience', 'maintenance'}
    commands = {'network-setup', 'experience', 'system', 'dhcp', 'probe', 'rotate-key'}
    cached = {'connection': None, 'state': None}

    def connection():
        cfg = netem.traffic_generator_config()
        return (cfg.get('host'), cfg.get('port'), cfg.get('tls_sha256'))

    def snapshot():
        state = netem.traffic_generator_request('/api/v1/controller/settings', timeout=10)
        # One verified path inventory for native configuration and test preflight.
        with netem.APPLIANCE_PLATFORM.operations:
            netem.APPLIANCE_PLATFORM.store.state('traffic_paths', {'paths': state['network']['paths']})
        cached.update(connection=connection(), state=state)
        return state

    def page(section, state=None, error=None, entered=None, status=200):
        if state is None and error is None:
            try:
                state = snapshot()
            except (RuntimeError, ValueError, KeyError) as exc:
                error = str(exc)
                state = cached['state'] if cached['connection'] == connection() else None
        state = state or {}
        system = state.get('system') or {}
        busy = bool(error or system.get('busy') or system.get('workload_active') or netem.scenario_snapshot().get('active'))
        return render_template('traffic_settings.html', page='settings', section=section, executor=state,
            system=system, network=state.get('network') or {}, experience=state.get('experience') or {},
            busy=busy, error=error, entered=entered or {}, path_links=netem.load_config().get('wan_links', [])), status

    @app.get('/settings/traffic/<section>')
    def traffic_settings(section):
        if section not in sections:
            abort(404)
        return page(section)

    @app.get('/api/v1/traffic/settings')
    def traffic_settings_data():
        try:
            return jsonify(snapshot())
        except (RuntimeError, ValueError, KeyError) as exc:
            return {'error': str(exc)}, 502

    @app.post('/settings/traffic/<section>/<command>')
    def traffic_settings_command(section, command):
        if section not in sections or command not in commands:
            abort(404)
        supplied, expected = request.form.get('integration_csrf', ''), session.get('integration_csrf', '')
        if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            abort(400, 'Invalid form token. Reload the page and retry.')
        raw = {key: value for key, value in request.form.items() if key != 'integration_csrf'}
        try:
            with netem.APPLIANCE_PLATFORM.operations:
                if netem.scenario_snapshot().get('active'):
                    raise ValueError('This test controls traffic. Stop & restore the test before changing configuration.')
                if command == 'experience':
                    policy = snapshot()['experience']['policy']
                    raw = {name: {key: request.form.get(name + '.' + key) == 'on' if key == 'critical'
                        else request.form.get(name + '.' + key) for key in fields} for name, fields in policy.items()}
                elif command == 'probe':
                    raw = {'gateways': [{'kind': kind, 'gateway': raw[kind + '_gateway']}
                        for kind in ('appliance', 'isp1', 'isp2') if raw.get(kind + '_gateway')]}
                reply = netem.traffic_generator_request('/api/v1/controller/commands/' + command,
                    method='POST', payload=raw, timeout=10)
                if command == 'rotate-key' and reply.get('api_key'):
                    values = netem.load_secrets()
                    values['traffic_generator_api_key'] = reply['api_key']
                    netem.save_secrets(values)
                netem.TRAFFIC_PATH_CACHE.update(checked=None, payload=None)
                netem.log_event('traffic-configuration', reply.get('message') or 'Traffic configuration accepted', command=command)
                flash(reply.get('message') or 'Task queued. Follow progress below.', 'warning' if raw.get('action') == 'save_interface' else 'success')
            return redirect(url_for('traffic_settings', section=section))
        except (RuntimeError, ValueError, KeyError) as exc:
            # Retain entered values so validation never discards operator work.
            try:
                state = snapshot()
            except (RuntimeError, ValueError, KeyError):
                state = cached['state'] if cached['connection'] == connection() else {}
            return page(section, state=state, error=str(exc), entered=request.form.to_dict(), status=400)
