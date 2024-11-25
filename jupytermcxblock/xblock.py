import logging
import os
import urllib.parse

from django.conf import settings
from lti_consumer.lti_xblock import LtiConsumerXBlock, LtiError, track_event, _
from webob import Response
from xblock.core import Scope, String, XBlock
from xblockutils.resources import ResourceLoader
try:
    from xblock.utils.resources import ResourceLoader
except ModuleNotFoundError:  # For backward compatibility with releases older than Quince.
    from xblockutils.resources import ResourceLoader
LTI_1P1_ROLE_MAP = {
    'student': 'Student,Learner',
    'staff': 'Administrator',
    'instructor': 'Instructor',
}


logger = logging.getLogger(__name__)


class JupyterMCXBlock(LtiConsumerXBlock):
    # Advanced component name
    display_name = String(
        display_name=_("Display Name"),
        help=_(
            "Enter the name that students see for this component. "
            "Analytics reports may also use the display name to identify this component."
        ),
        scope=Scope.settings,
        default=_("JupyterHub"),
    )

    # which interface to open
    urlbasepath = String(
        display_name=_("Interface to use"),
        scope=Scope.settings,
        values=[
            {"display_name": "Jupyter Notebook", "value": "tree"},
            {"display_name": "JupyterLab", "value": "lab/tree"},
            {"display_name": "RStudio", "value": "rstudio"},
            {"display_name": "Terminal", "value": "terminals/1"}
            {"display_name": "Desktop", "value": "Desktop"}
            {"display_name": "OpenRefine", "value": "openrefine"}
            {"display_name": "VS Code", "value": "code-server"}
        ],
        default="lab/tree",
        help=_(
            "Select the interface that the notebook should open."
        ),
    )

    # Jupyter git repo attributes
    nb_git_repo = String(
        display_name=_("Notebook git repository"),
        help="For example: https://github.com/calculquebec/jupytermc-xblock.git",
        default="https://github.com/calculquebec/jupytermc-xblock.git",
        scope=Scope.settings,
    )
    nb_git_branch = String(
        display_name=_("Notebook git branch"),
        default="main",
        scope=Scope.settings,
    )
    nb_git_file = String(
        display_name=_("Notebook file"),
        help="Path relative to the repository root",
        default="static/notebooks/hello.ipynb",
        scope=Scope.settings,
    )

    # LTI attributes
    lti_id = String(
        display_name=_("LTI ID"),
        help=LtiConsumerXBlock.lti_id.help,
        default=getattr(
            settings,
            "LTI_DEFAULT_JUPYTER_PASSPORT_ID",
            getattr(settings, "LTI_DEFAULT_PASSPORT_ID", "jupyterhub"),
        ),
        scope=Scope.settings,
    )
    hub_url = String(
        display_name=_("JupyterHub base URL"),
        help="""For example: https://hub.myopenedx.com""",
        default=getattr(
            settings,
            "LTI_DEFAULT_JUPYTER_HUB_URL",
            f"https://hub.{settings.LMS_BASE}",
        ),
        scope=Scope.settings,
    )

    # Limit the number of editable fields
    editable_field_names = (
        "display_name",
        "urlbasepath",
        "launch_target",
        "hub_url",
        "lti_id",
        "nb_git_repo",
        "nb_git_branch",
        "nb_git_file",
    )

    # Override base attributes
    @property
    def launch_url(self):
        """Infer launch URL from JupyterHub URL"""
        return f"{self.hub_url}/hub/lti/launch"

    @property
    def launch_target(self):
        """If RStudio is used, launch target must be new window"""
        if self.urlbasepath in ["rstudio", "openrefine", "Desktop", "code-server"]:
            return "new_window"
        else:
            return super().launch_target

    @property
    def lti_version(self):
        """Always use LTI 1.1"""
        return "lti_1p1"

    @property
    def hub_url_base_path(self):
        path = urllib.parse.urlparse(self.hub_url).path
        return path.strip("/")

    @property
    def prefixed_custom_parameters(self):
        custom_parameters = super().prefixed_custom_parameters

        # Override `next=` custom parameter to follow the specs from nbgitpuller.
        # See: https://hub.jupyter.org/nbgitpuller/link
        next_query_params = {
            "repo": self.nb_git_repo,
            "branch": self.nb_git_branch,
            "urlpath": f"{self.urlbasepath}/{os.path.basename(self.nb_git_repo)}/{self.nb_git_file}",
        }

        # in the case of terminals, rstudio, openrefine, Desktop, code-server, we can't open to a specific location, the notebook to open is irrelevant
        if self.urlbasepath in ["terminals/1", "rstudio", "openrefine", "Desktop", "code-server"]:
            next_query_params['urlpath'] = f"{self.urlbasepath}"

        logger.info(
            "Fetching git repo=%s, branch=%s, urlpath=%s",
            next_query_params["repo"],
            next_query_params["branch"],
            next_query_params["urlpath"],
        )
        next_url = f"{self.hub_url_base_path}/hub/user-redirect/git-pull?{urllib.parse.urlencode(next_query_params)}"
        custom_parameters["next"] = next_url

        return custom_parameters

    @XBlock.handler
    def lti_launch_handler(self, request, suffix=''):  # pylint: disable=unused-argument
        """
        XBlock handler for launching LTI 1.1 tools.

        Displays a form which is submitted via Javascript
        to send the LTI launch POST request to the LTI
        provider.

        Arguments:
            request (xblock.django.request.DjangoWebobRequest): Request object for current HTTP request
            suffix (unicode): Request path after "lti_launch_handler/"

        Returns:
            webob.response: HTML LTI launch form
        """
        lti_consumer = self._get_lti_consumer()

        # Occassionally, users try to do an LTI launch while they are unauthenticated. It is not known why this occurs.
        # Sometimes, it is due to a web crawlers; other times, it is due to actual users of the platform. Regardless,
        # return a 400 response with an appropriate error template.
        try:
            real_user_data = self.extract_real_user_data()
            user_id = self.get_lti_1p1_user_id()
            role = self.role

            # Convert the LMS role into an LTI 1.1 role.
            role = LTI_1P1_ROLE_MAP.get(role, 'Student,Learner')

            result_sourcedid = self.lis_result_sourcedid
        # Fails if extract_real_user_data() fails
        except LtiError as err:
            loader = ResourceLoader(__name__)
            template = loader.render_django_template('/templates/html/lti_launch_error.html',
                                                     context={"error_msg": err})
            return Response(template, status=400, content_type='text/html')

        username = None
        full_name = None
        email = None

        # Always send username as this is needed
        if real_user_data['user_username']:
            username = real_user_data['user_username']

        lti_consumer.set_user_data(
            user_id,
            role,
            result_sourcedid=result_sourcedid,
            person_sourcedid=username,
            person_contact_email_primary=email,
            person_name_full=full_name,
        )

        lti_consumer.set_context_data(
            self.context_id,
            self.course.display_name_with_default,
            self.course.display_org_with_default
        )

        if self.has_score:
            lti_consumer.set_outcome_service_url(self.outcome_service_url)

        if real_user_data['user_language']:
            lti_consumer.set_launch_presentation_locale(real_user_data['user_language'])

        lti_consumer.set_custom_parameters(self.prefixed_custom_parameters)

        for processor in self.get_parameter_processors():
            try:
                default_params = getattr(processor, 'lti_xblock_default_params', {})
                lti_consumer.set_extra_claims(default_params)
                lti_consumer.set_extra_claims(processor(self) or {})
            except Exception:  # pylint: disable=broad-except
                # Log the error without causing a 500-error.
                # Useful for catching casual runtime errors in the processors.
                log.exception('Error in XBlock LTI parameter processor "%s"', processor)

        lti_parameters = lti_consumer.generate_launch_request(self.resource_link_id)

        # emit tracking event
        event = {
            'lti_version': lti_parameters.get('lti_version'),
            'user_roles': lti_parameters.get('roles'),
            'launch_url': lti_consumer.lti_launch_url,
        }
        track_event('xblock.launch_request', event)

        loader = ResourceLoader(__name__)
        context = self._get_context_for_template()
        context.update({'lti_parameters': lti_parameters})
        template = loader.render_django_template('/templates/html/lti_launch.html', context)
        return Response(template, content_type='text/html')

    # Fix student view
    @XBlock.supports("multi_device")
    def student_view(self, context):
        """
        Fix CSS, as the CSS rules defined in the base LTI XBlock do not apply to this
        one.
        """
        fragment = super().student_view(context)
        loader = ResourceLoader(__name__)
        fragment.add_css(loader.load_unicode("static/css/student.css"))
        return fragment
