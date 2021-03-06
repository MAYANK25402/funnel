from flask import abort, g, redirect, request

from baseframe import _, forms
from baseframe.forms import Form, render_form
from coaster.auth import current_auth
from coaster.views import (
    ModelView,
    UrlChangeCheck,
    UrlForView,
    render_with,
    requires_roles,
    route,
)

from .. import app, funnelapp, signals
from ..forms import (
    OrganizationMembershipForm,
    ProjectCrewMembershipForm,
    ProjectCrewMembershipInviteForm,
)
from ..models import (
    MembershipRevokedError,
    Organization,
    OrganizationAdminMembershipNotification,
    OrganizationAdminMembershipRevokedNotification,
    OrganizationMembership,
    Profile,
    Project,
    ProjectCrewMembership,
    db,
)
from .decorators import legacy_redirect
from .login_session import requires_login, requires_sudo
from .mixins import ProfileViewMixin, ProjectViewMixin
from .notification import dispatch_notification


@Profile.views('members')
@route('/<profile>/members')
class OrganizationMembersView(ProfileViewMixin, UrlForView, ModelView):
    __decorators__ = [legacy_redirect]

    @route('', methods=['GET', 'POST'])
    @render_with('organization_membership.html.jinja2')
    def members(self):
        if not self.obj.organization:
            # User profiles don't have memberships
            abort(404)
        return {
            'profile': self.obj,
            'memberships': [
                membership.current_access(datasets=('without_parent', 'related'))
                for membership in self.obj.organization.active_admin_memberships
            ],
        }

    @route('new', methods=['GET', 'POST'])
    @render_with(json=True)
    @requires_login
    @requires_roles({'owner'})
    def new_member(self):
        if not self.obj.organization:
            # User profiles don't have memberships
            abort(404)
        membership_form = OrganizationMembershipForm()

        if request.method == 'POST':
            if membership_form.validate_on_submit():
                if not membership_form.user.data.has_verified_contact_info:
                    # users without verified contact information cannot be members
                    return (
                        {
                            'status': 'error',
                            'error_description': _(
                                "This user does not have any verified contact"
                                " information. If you are able to contact them, please"
                                " ask them to verify their email address or phone"
                                " number"
                            ),
                            'errors': membership_form.errors,
                            'form_nonce': membership_form.form_nonce.data,
                        },
                        400,
                    )

                previous_membership = (
                    OrganizationMembership.query.filter(
                        OrganizationMembership.is_active
                    )
                    .filter_by(
                        organization=self.obj.organization,
                        user_id=membership_form.user.data.id,
                    )
                    .one_or_none()
                )
                if previous_membership is not None:
                    return (
                        {
                            'status': 'error',
                            'error_description': _("This user is already an admin"),
                            'errors': membership_form.errors,
                            'form_nonce': membership_form.form_nonce.data,
                        },
                        400,
                    )
                else:
                    new_membership = OrganizationMembership(
                        organization=self.obj.organization, granted_by=current_auth.user
                    )
                    membership_form.populate_obj(new_membership)
                    db.session.add(new_membership)
                    db.session.commit()
                    dispatch_notification(
                        OrganizationAdminMembershipNotification(
                            document=new_membership.organization,
                            fragment=new_membership,
                        )
                    )
                    return {
                        'status': 'ok',
                        'message': _("The user has been added as an admin"),
                        'memberships': [
                            membership.current_access(
                                datasets=('without_parent', 'related')
                            )
                            for membership in self.obj.organization.active_admin_memberships
                        ],
                    }
            else:
                return (
                    {
                        'status': 'error',
                        'error_description': _("The new admin could not be added"),
                        'errors': membership_form.errors,
                        'form_nonce': membership_form.form_nonce.data,
                    },
                    400,
                )

        membership_form_html = render_form(
            form=membership_form,
            title='',
            submit=u'Add member',
            ajax=False,
            with_chrome=False,
        )
        return {'form': membership_form_html}


OrganizationMembersView.init_app(app)


@OrganizationMembership.views('main')
@route('/<profile>/members/<membership>')
class OrganizationMembershipView(UrlChangeCheck, UrlForView, ModelView):
    model = OrganizationMembership
    __decorators__ = [legacy_redirect]

    route_model_map = {'profile': 'organization.name', 'membership': 'uuid_b58'}

    def loader(self, profile, membership):
        obj = (
            self.model.query.join(Organization, Profile)
            .filter(
                OrganizationMembership.uuid_b58 == membership,
                OrganizationMembership.organization_id == Profile.organization_id,
                db.func.lower(Profile.name) == db.func.lower(profile),
            )
            .first_or_404()
        )
        return obj

    def after_loader(self):
        g.profile = self.obj.organization.profile
        super().after_loader()

    @route('edit', methods=['GET', 'POST'])
    @render_with(json=True)
    @requires_login
    @requires_roles({'profile_owner'})
    def edit(self):
        previous_membership = self.obj
        membership_form = OrganizationMembershipForm(obj=previous_membership)

        if request.method == 'POST':
            if membership_form.validate_on_submit():
                if previous_membership.user == current_auth.user:
                    return {
                        'status': 'error',
                        'error_description': _("You can’t edit your own role"),
                        'form_nonce': membership_form.form_nonce.data,
                    }

                try:
                    new_membership = previous_membership.replace(
                        actor=current_auth.user, is_owner=membership_form.is_owner.data
                    )
                except MembershipRevokedError:
                    return (
                        {
                            'status': 'error',
                            'error_description': _(
                                "This member’s record was edited elsewhere."
                                " Please refresh the page"
                            ),
                            'form_nonce': membership_form.form_nonce.data,
                        },
                        400,
                    )
                if new_membership != previous_membership:
                    db.session.commit()
                    dispatch_notification(
                        OrganizationAdminMembershipNotification(
                            document=new_membership.organization,
                            fragment=new_membership,
                        )
                    )
                return {
                    'status': 'ok',
                    'message': (
                        _("The member’s roles have been updated")
                        if new_membership != previous_membership
                        else _("No changes were detected")
                    ),
                    'memberships': [
                        membership.current_access(
                            datasets=('without_parent', 'related')
                        )
                        for membership in self.obj.organization.active_admin_memberships
                    ],
                }
            else:
                return (
                    {
                        'status': 'error',
                        'error_description': _("Please pick one or more roles"),
                        'errors': membership_form.errors,
                        'form_nonce': membership_form.form_nonce.data,
                    },
                    400,
                )

        membership_form_html = render_form(
            form=membership_form,
            title='',
            submit=u'Edit membership',
            ajax=False,
            with_chrome=False,
        )
        return {'form': membership_form_html}

    @route('delete', methods=['GET', 'POST'])
    @render_with(json=True)
    @requires_sudo
    @requires_roles({'profile_owner'})
    def delete(self):
        form = Form()
        if request.method == 'POST':
            if form.validate_on_submit():
                previous_membership = self.obj
                if previous_membership.user == current_auth.user:
                    return {
                        'status': 'error',
                        'error_description': _("You can’t revoke your own membership"),
                        'form_nonce': form.form_nonce.data,
                    }
                if previous_membership.is_active:
                    previous_membership.revoke(actor=current_auth.user)
                    db.session.commit()
                    dispatch_notification(
                        OrganizationAdminMembershipRevokedNotification(
                            document=previous_membership.organization,
                            fragment=previous_membership,
                        )
                    )
                return {
                    'status': 'ok',
                    'message': _("The member has been removed"),
                    'memberships': [
                        membership.current_access(
                            datasets=('without_parent', 'related')
                        )
                        for membership in self.obj.organization.active_admin_memberships
                    ],
                }
            else:
                return (
                    {
                        'status': 'error',
                        'errors': form.errors,
                        'form_nonce': form.form_nonce.data,
                    },
                    400,
                )

        form_html = render_form(
            form=form,
            title=_("Confirm removal"),
            message=_("Remove {member} as an admin from {profile}?").format(
                member=self.obj.user.fullname, profile=self.obj.organization.title
            ),
            submit=_("Remove"),
            ajax=False,
            with_chrome=False,
        )
        return {'form': form_html}


OrganizationMembershipView.init_app(app)


#: Project Membership views


@Project.views('crew')
@route('/<profile>/<project>/crew')
class ProjectMembershipView(ProjectViewMixin, UrlChangeCheck, UrlForView, ModelView):
    __decorators__ = [legacy_redirect]

    @route('', methods=['GET', 'POST'])
    @render_with('project_membership.html.jinja2')
    def crew(self):
        return {
            'project': self.obj,
            'memberships': [
                membership.current_access(datasets=('without_parent', 'related'))
                for membership in self.obj.active_crew_memberships
            ],
            'csrf_form': forms.Form(),
        }

    @route('new', methods=['GET', 'POST'])
    @render_with(json=True)
    @requires_login
    @requires_roles({'profile_admin'})
    def new_member(self):
        membership_form = ProjectCrewMembershipForm()

        if request.method == 'POST':
            if membership_form.validate_on_submit():
                if not membership_form.user.data.has_verified_contact_info:
                    # users without verified contact information cannot be members
                    return (
                        {
                            'status': 'error',
                            'error_description': _(
                                "This user does not have any verified contact"
                                " information. If you are able to contact them, please"
                                " ask them to verify their email address or phone"
                                " number"
                            ),
                            'errors': membership_form.errors,
                            'form_nonce': membership_form.form_nonce.data,
                        },
                        400,
                    )
                previous_membership = (
                    ProjectCrewMembership.query.filter(ProjectCrewMembership.is_active)
                    .filter_by(project=self.obj, user_id=membership_form.user.data.id)
                    .one_or_none()
                )
                if previous_membership is not None:
                    return (
                        {
                            'status': 'error',
                            'error_description': _("This person is already a member"),
                            'errors': membership_form.errors,
                            'form_nonce': membership_form.form_nonce.data,
                        },
                        400,
                    )
                else:
                    new_membership = ProjectCrewMembership(
                        project=self.obj, granted_by=current_auth.user
                    )
                    membership_form.populate_obj(new_membership)
                    db.session.add(new_membership)
                    # TODO: Once invite is introduced, send invite email here
                    signals.project_crew_membership_added.send(
                        self.obj,
                        project=self.obj,
                        membership=new_membership,
                        actor=current_auth.user,
                        user=new_membership.user,
                    )
                    db.session.commit()
                    return {
                        'status': 'ok',
                        'message': _("The user has been added as a member"),
                        'memberships': [
                            membership.current_access(
                                datasets=('without_parent', 'related')
                            )
                            for membership in self.obj.active_crew_memberships
                        ],
                    }
            else:
                return (
                    {
                        'status': 'error',
                        'error_description': _("The new member could not be added"),
                        'errors': membership_form.errors,
                        'form_nonce': membership_form.form_nonce.data,
                    },
                    400,
                )

        membership_form_html = render_form(
            form=membership_form,
            title='',
            submit=u'Add member',
            ajax=False,
            with_chrome=False,
        )
        return {'form': membership_form_html}


@route('/<project>/crew', subdomain='<profile>')
class FunnelProjectMembershipView(ProjectMembershipView):
    pass


ProjectMembershipView.init_app(app)
FunnelProjectMembershipView.init_app(funnelapp)


class ProjectCrewMembershipMixin(object):
    model = ProjectCrewMembership

    route_model_map = {
        'profile': 'project.profile.name',
        'project': 'project.name',
        'membership': 'uuid_b58',
    }

    def loader(self, profile, project, membership):
        obj = (
            self.model.query.join(Project, Profile)
            .filter(
                db.func.lower(Profile.name) == db.func.lower(profile),
                Project.name == project,
                ProjectCrewMembership.uuid_b58 == membership,
            )
            .first_or_404()
        )
        return obj

    def after_loader(self):
        g.profile = self.obj.project.profile
        super(ProjectCrewMembershipMixin, self).after_loader()


@ProjectCrewMembership.views('invite')
@route('/<profile>/<project>/crew/<membership>/invite')
class ProjectCrewMembershipInviteView(
    ProjectCrewMembershipMixin, UrlChangeCheck, UrlForView, ModelView
):
    __decorators__ = [legacy_redirect]

    def loader(self, profile, project, membership):
        obj = super().loader(profile, project, membership)
        if not obj.is_invite or obj.user != current_auth.user:
            raise abort(404)

        return obj

    @route('', methods=['GET'])
    @render_with('membership_invite_actions.html.jinja2')
    @requires_login
    def invite(self):
        return {
            'membership': self.obj.current_access(datasets=('primary', 'related')),
            'form': Form(),
        }

    @route('action', methods=['POST'])
    @requires_login
    def invite_action(self):
        membership_invite_form = ProjectCrewMembershipInviteForm()
        if membership_invite_form.validate_on_submit():
            if membership_invite_form.action.data == 'accept':
                self.obj.accept(actor=current_auth.user)
            elif membership_invite_form.action.data == 'decline':
                self.obj.revoke(actor=current_auth.user)
            db.session.commit()
        return redirect(self.obj.project.url_for(), 303)


@route('/<project>/crew/<membership>/invite', subdomain='<profile>')
class FunnelProjectCrewMembershipInviteView(ProjectCrewMembershipInviteView):
    pass


ProjectCrewMembershipInviteView.init_app(app)
FunnelProjectCrewMembershipInviteView.init_app(funnelapp)


@ProjectCrewMembership.views('main')
@route('/<profile>/<project>/crew/<membership>')
class ProjectCrewMembershipView(
    ProjectCrewMembershipMixin, UrlChangeCheck, UrlForView, ModelView
):
    __decorators__ = [legacy_redirect]

    @route('edit', methods=['GET', 'POST'])
    @render_with(json=True)
    @requires_login
    @requires_roles({'profile_admin'})
    def edit(self):
        previous_membership = self.obj
        membership_form = ProjectCrewMembershipForm(obj=previous_membership)

        if request.method == 'POST':
            if membership_form.validate_on_submit():
                try:
                    previous_membership.replace(
                        actor=current_auth.user,
                        is_editor=membership_form.is_editor.data,
                        is_concierge=membership_form.is_concierge.data,
                        is_usher=membership_form.is_usher.data,
                    )
                except MembershipRevokedError:
                    return (
                        {
                            'status': 'error',
                            'error_description': _(
                                "The member’s record was edited elsewhere."
                                " Please refresh the page"
                            ),
                            'form_nonce': membership_form.form_nonce.data,
                        },
                        400,
                    )
                db.session.commit()
                return {
                    'status': 'ok',
                    'message': _("The member’s roles have been updated"),
                    'memberships': [
                        membership.current_access(
                            datasets=('without_parent', 'related')
                        )
                        for membership in self.obj.project.active_crew_memberships
                    ],
                }
            else:
                return (
                    {
                        'status': 'error',
                        'error_description': _("Please pick one or more roles"),
                        'errors': membership_form.errors,
                        'form_nonce': membership_form.form_nonce.data,
                    },
                    400,
                )

        membership_form_html = render_form(
            form=membership_form,
            title='',
            submit=u'Edit membership',
            ajax=False,
            with_chrome=False,
        )
        return {'form': membership_form_html}

    @route('delete', methods=['GET', 'POST'])
    @render_with(json=True)
    @requires_sudo
    @requires_roles({'profile_admin'})
    def delete(self):
        form = Form()
        if request.method == 'POST':
            if form.validate_on_submit():
                previous_membership = self.obj
                if previous_membership.is_active:
                    previous_membership.revoke(actor=current_auth.user)
                    signals.project_crew_membership_revoked.send(
                        self.obj.project,
                        project=self.obj.project,
                        membership=previous_membership,
                        actor=current_auth.user,
                        user=previous_membership.user,
                    )
                    db.session.commit()
                return {
                    'status': 'ok',
                    'message': _("The member has been removed"),
                    'memberships': [
                        membership.current_access(
                            datasets=('without_parent', 'related')
                        )
                        for membership in self.obj.project.active_crew_memberships
                    ],
                }
            else:
                return ({'status': 'error', 'errors': form.errors}, 400)

        form_html = render_form(
            form=form,
            title=_("Confirm removal"),
            message=_("Remove {member} as a crew member from this project?").format(
                member=self.obj.user.fullname
            ),
            submit=_("Remove"),
            ajax=False,
            with_chrome=False,
        )
        return {'form': form_html}


@route('/<project>/crew/<membership>', subdomain='<profile>')
class FunnelProjectCrewMembershipView(ProjectCrewMembershipView):
    pass


ProjectCrewMembershipView.init_app(app)
FunnelProjectCrewMembershipView.init_app(funnelapp)
