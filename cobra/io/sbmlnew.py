"""
SBML import and export using libsbml.


TODO: converters
- COBRA to FBCV2
- FBCV2 to COBRA
- FBCV1 to FBCV2

- SBMLIdConverter

"""
# -------------------------------
# TODO
# ------------------------------
# [1] Replacing/Changing of identifiers between SBML and cobra formats
# clip ids
# clip(met, "M_")
# clip_prefixes = {'compartment': None, 'specie': 'M_', 'gene': 'G_'}
# replace ids
# get_attrib(sbml_gene, "fbc:id").replace(SBML_DOT, ".")

# [2] Legacy format and COBRA format support
# [3] Conversion of FBCv1 to FBCv2


from __future__ import absolute_import

import os
from warnings import warn
from six import string_types
from collections import defaultdict, namedtuple

import libsbml
from cobra.core import Gene, Metabolite, Model, Reaction
from cobra.util.solver import set_objective

try:
    from lxml.etree import (
        parse, Element, SubElement, ElementTree, register_namespace,
        ParseError, XPath)

    _with_lxml = True
except ImportError:
    _with_lxml = False


LONG_SHORT_DIRECTION = {'maximize': 'max', 'minimize': 'min'}
SHORT_LONG_DIRECTION = {'min': 'minimize', 'max': 'maximize'}

LOWER_BOUND = -1000
UPPER_BOUND = 1000
LOWER_BOUND_ID = "cobra_default_lb"
UPPER_BOUND_ID = "cobra_default_ub"
ZERO_BOUND_ID = "cobra_0_bound"

Unit = namedtuple('Unit', ['kind', 'scale', 'multiplier', 'exponent'])
UNITS_FLUX = ("mmol_per_gDW_per_hr",
              [Unit(kind='mole', scale=-3, multiplier=1, exponent=1),
               Unit(kind='gram', scale=0, multiplier=1, exponent=-1),
               Unit(kind='second', scale=0, multiplier=3600, exponent=-1)]
              )


class CobraSBMLError(Exception):
    """ SBML error class. """
    pass


def read_sbml_model(filename):
    """ Reads model from given filename.

    If the given filename ends with the suffix ''.gz'' (for example,
    ''myfile.xml.gz'),' the file is assumed to be compressed in gzip
    format and will be automatically decompressed upon reading. Similarly,
    if the given filename ends with ''.zip'' or ''.bz2',' the file is
    assumed to be compressed in zip or bzip2 format (respectively).  Files
    whose names lack these suffixes will be read uncompressed.  Note that
    if the file is in zip format but the archive contains more than one
    file, only the first file in the archive will be read and the rest
    ignored.

    To read a gzip/zip file, libSBML needs to be configured and linked
    with the zlib library at compile time.  It also needs to be linked
    with the bzip2 library to read files in bzip2 format.  (Both of these
    are the default configurations for libSBML.)

    :param filename: path to SBML file or SBML string
    :param validate: validate the file on reading (additional overhead)
    :return:
    """
    try:
        if os.path.exists(filename):
            doc = libsbml.readSBMLFromFile(filename)  # type: libsbml.SBMLDocument
        elif isinstance(filename, string_types):
            # SBML as string representation
            doc = libsbml.readSBMLFromString(filename)
        elif hasattr(filename, "read"):
            # File handle
            doc = libsbml.readSBMLFromString(filename.read())
        else:
            raise CobraSBMLError(
                "Input format is not supported."
            )
        # FIXME: check SBML parser errors

        return _sbml_to_model(doc)

    except Exception:
        raise CobraSBMLError(
            "Something went wrong reading the model. You can get a detailed "
            "report using the `cobra.io.sbml3.validate_sbml_model` function "
            "or using the online validator at http://sbml.org/validator")


def write_sbml_model(cobra_model, filename, **kwargs):
    """ Writes cobra model to filename.

    If the given filename ends with the suffix ".gz" (for example,
    "myfile.xml.gz"), libSBML assumes the caller wants the file to be
    written compressed in gzip format. Similarly, if the given filename
    ends with ".zip" or ".bz2", libSBML assumes the caller wants the
    file to be compressed in zip or bzip2 format (respectively). Files
    whose names lack these suffixes will be written uncompressed. Special
    considerations for the zip format: If the given filename ends with
    ".zip", the file placed in the zip archive will have the suffix
    ".xml" or ".sbml".  For example, the file in the zip archive will
    be named "test.xml" if the given filename is "test.xml.zip" or
    "test.zip". Similarly, the filename in the archive will be
    "test.sbml" if the given filename is "test.sbml.zip".

    :param cobra_model:
    :param filename:
    :param use_fbc_package:
    :param kwargs:
    :return:
    """

    # TODO: legacy SBML
    # if not use_fbc_package:
    #     if libsbml is None:
    #         raise ImportError("libSBML required to write non-fbc models")
    #     write_sbml2(cobra_model, filename, use_fbc_package=False, **kwargs)
    #     return

    # create xml
    doc = _model_to_sbml(cobra_model, **kwargs)
    libsbml.writeSBMLToFile(doc, filename)


def _sbml_to_model(doc, number=float):
    """ Creates cobra model from SBMLDocument.

    :param doc: libsbml.SBMLDocument
    'param number: data type of stoichiometry
    :return: cobrapy model
    """
    # SBML model
    doc_fbc = doc.getPlugin("fbc")  # type: libsbml.FbcSBMLDocumentPlugin
    model = doc.getModel()  # type: libsbml.Model
    model_fbc = model.getPlugin("fbc")  # type: libsbml.FbcModelPlugin

    if not model_fbc:
        warn("Model does not contain FBC information.")
    else:
        if not model_fbc.isSetStrict():
            warn('Loading SBML model without fbc:strict="true"')

    # Model
    cmodel = Model(model.id)
    cmodel.name = model.name

    # Compartments
    cmodel.compartments = {c.id: c.name for c in model.compartments}

    # Species
    boundary_ids = set()
    for s in model.species:  # type: libsbml.Species
        sid = _check_required(s, s.id, "id")
        met = Metabolite(sid)
        met.name = s.name
        met.compartment = s.compartment
        s_fbc = s.getPlugin("fbc")
        if s_fbc:
            met.charge = s_fbc.getCharge()
            met.formula = s_fbc.getChemicalFormula()

        # Detect boundary metabolites - In case they have been mistakenly
        # added. They should not actually appear in a model
        if s.getBoundaryCondition() is True:
            boundary_ids.add(s.id)

        annotate_cobra_from_sbase(met, s)

        cmodel.add_metabolites([met])

    # Genes
    for gp in model_fbc.getListOfGeneProducts():  # type: libsbml.GeneProduct
        gene = Gene(gp.id)
        gene.name = gp.name
        if gene.name is None:
            gene.name = gp.get
        annotate_cobra_from_sbase(gene, gp)
        cmodel.genes.append(gene)

    # Reactions
    reactions = []
    for r in model.reactions:  # type: libsbml.Reaction
        rid = _check_required(r, r.id, "id")
        reaction = Reaction(rid)
        reaction.name = r.name
        annotate_cobra_from_sbase(reaction, r)

        # set bounds
        r_fbc = r.getPlugin("fbc")  # type: libsbml.FbcReactionPlugin
        if r_fbc is None:
            raise CobraSBMLError("No flux bounds on reaction '%s'" % r)
        else:
            # FIXME: remove code duplication in this section
            lb_id = _check_required(r_fbc, r_fbc.getLowerFluxBound(), "lowerFluxBound")
            ub_id = _check_required(r_fbc, r_fbc.getUpperFluxBound(), "upperFluxBound")
            p_lb = model.getParameter(lb_id)
            p_ub = model.getParameter(ub_id)

            if p_lb.constant and (p_lb.value is not None):
                reaction.lower_bound = p_lb.value
            else:
                raise CobraSBMLError("No constant bound '%s' for reaction '%s" % (p_lb, r))

            if p_ub.constant and (p_ub.value is not None):
                reaction.upper_bound = p_ub.value
            else:
                raise CobraSBMLError("No constant bound '%s' for reaction '%s" % (p_ub, r))
                bounds.append(p.value)

        reactions.append(reaction)

        # parse equation
        stoichiometry = defaultdict(lambda: 0)
        for sref in r.getListOfReactants():  # type: libsbml.SpeciesReference
            sid = sref.getSpecies()
            # FIXME: clip
            stoichiometry[sid] -= number(_check_required(sref, sref.stoichiometry, "stoichiometry"))

        for sref in r.getListOfProducts():  # type: libsbml.SpeciesReference
            sid = sref.getSpecies()
            stoichiometry[sid] += number(_check_required(sref, sref.stoichiometry, "stoichiometry"))

        # needs to have keys of metabolite objects, not ids
        object_stoichiometry = {}
        for met_id in stoichiometry:
            if met_id in boundary_ids:
                warn("Boundary metabolite '%s' used in reaction '%s'" %
                     (met_id, reaction.id))
                continue
            try:
                metabolite = cmodel.metabolites.get_by_id(met_id)
            except KeyError:
                warn("ignoring unknown metabolite '%s' in reaction %s" %
                     (met_id, reaction.id))
                continue
            object_stoichiometry[metabolite] = stoichiometry[met_id]
        reaction.add_metabolites(object_stoichiometry)


        # GPR rules
        # TODO
        '''
        def process_gpr(sub_xml):
            """recursively convert gpr xml to a gpr string"""
            if sub_xml.tag == OR_TAG:
                return "( " + ' or '.join(process_gpr(i) for i in sub_xml) + " )"
            elif sub_xml.tag == AND_TAG:
                return "( " + ' and '.join(process_gpr(i) for i in sub_xml) + " )"
            elif sub_xml.tag == GENEREF_TAG:
                gene_id = get_attrib(sub_xml, "fbc:geneProduct", require=True)
                return clip(gene_id, "G_")
            else:
                raise Exception("unsupported tag " + sub_xml.tag)
        

        def process_association(association):
            """ Recursively convert gpr xml to a gpr string. """
            type_code = association.getTypeCode()
            if association.isFbcOr():
                association.get

                return "( " + ' or '.join(process_gpa(i) for i in gpa.getCh) + " )"
            elif sub_xml.tag == AND_TAG:
                return "( " + ' and '.join(process_gpr(i) for i in sub_xml) + " )"
            elif sub_xml.tag == GENEREF_TAG:
                gene_id = get_attrib(sub_xml, "fbc:geneProduct", require=True)
                return clip(gene_id, "G_")
            else:
                raise Exception("unsupported tag " + sub_xml.tag)
        '''
        gpa = r_fbc.getGeneProductAssociation()  # type: libsbml.GeneProductAssociation
        # print(gpa)

        association = None
        if gpa is not None:
            association = gpa.getAssociation()  # type: libsbml.FbcAssociation
            # print(association)
            # print(association.getListOfAllElements())



        # gpr = process_association(association) if association is not None else ''
        gpr = ''

        # remove outside parenthesis, if any
        if gpr.startswith("(") and gpr.endswith(")"):
            gpr = gpr[1:-1].strip()

        # gpr = gpr.replace(SBML_DOT, ".")
        reaction.gene_reaction_rule = gpr

    try:
        cmodel.add_reactions(reactions)
    except ValueError as e:
        warn(str(e))

    # Objective
    obj_list = model_fbc.getListOfObjectives()  # type: libsbml.ListOfObjectives
    if obj_list is None:
        warn("listOfObjectives element not found")
    else:
        obj_id = obj_list.getActiveObjective()
        obj = model_fbc.getObjective(obj_id)  # type: libsbml.Objective
        obj_direction = LONG_SHORT_DIRECTION[obj.getType()]

        coefficients = {}

        for flux_obj in obj.getListOfFluxObjectives():  # type: libsbml.FluxObjective
            # FIXME: clip id
            rid = flux_obj.getReaction()
            try:
                objective_reaction = cmodel.reactions.get_by_id(rid)
            except KeyError:
                raise CobraSBMLError("Objective reaction '%s' not found" % rid)
            try:
                coefficients[objective_reaction] = number(flux_obj.getCoefficient())
            except ValueError as e:
                warn(str(e))
        set_objective(cmodel, coefficients)
        cmodel.solver.objective.direction = obj_direction

    return cmodel



SBO_FBA_FRAMEWORK = "SBO:0000624"
SBO_FLUX_BOUND = "SBO:0000626"


def _model_to_sbml(cobra_model, units=True):
    """

    :param cobra_model:
    :param units: boolean, if true the FLUX_UNITS are written
    :return:
    """
    sbmlns = libsbml.SBMLNamespaces(3, 1)
    sbmlns.addPackageNamespace("fbc", 2)

    doc = libsbml.SBMLDocument(sbmlns)
    doc.setPackageRequired("fbc", False)
    doc.setSBOTerm(SBO_FBA_FRAMEWORK)
    model = doc.createModel()
    model_fbc = model.getPlugin("fbc")
    model_fbc.setStrict(True)

    # model
    model.setId('{}_fba'.format(model_id))
    model.setName('{} (FBA)'.format(model_id))
    model.setSBOTerm(comp.SBO_FLUX_BALANCE_FRAMEWORK)
    return doc


    xml = Element("sbml", xmlns=namespaces["sbml"], level="3", version="1",
                  sboTerm="SBO:0000624")
    set_attrib(xml, "fbc:required", "false")
    xml_model = SubElement(xml, "model")
    set_attrib(xml_model, "fbc:strict", "true")
    if cobra_model.id is not None:
        xml_model.set("id", cobra_model.id)
    if cobra_model.name is not None:
        xml_model.set("name", cobra_model.name)

    # if using units, add in mmol/gdw/hr
    if units:
        unit_def = SubElement(
            SubElement(xml_model, "listOfUnitDefinitions"),
            "unitDefinition", id="mmol_per_gDW_per_hr")
        list_of_units = SubElement(unit_def, "listOfUnits")
        SubElement(list_of_units, "unit", kind="mole", scale="-3",
                   multiplier="1", exponent="1")
        SubElement(list_of_units, "unit", kind="gram", scale="0",
                   multiplier="1", exponent="-1")
        SubElement(list_of_units, "unit", kind="second", scale="0",
                   multiplier="3600", exponent="-1")

    # create the element for the flux objective
    obj_list_tmp = SubElement(xml_model, ns("fbc:listOfObjectives"))
    set_attrib(obj_list_tmp, "fbc:activeObjective", "obj")
    obj_list_tmp = SubElement(obj_list_tmp, ns("fbc:objective"))
    set_attrib(obj_list_tmp, "fbc:id", "obj")
    set_attrib(obj_list_tmp, "fbc:type",
               SHORT_LONG_DIRECTION[cobra_model.objective.direction])
    flux_objectives_list = SubElement(obj_list_tmp,
                                      ns("fbc:listOfFluxObjectives"))

    # create the element for the flux bound parameters
    parameter_list = SubElement(xml_model, "listOfParameters")
    param_attr = {"constant": "true"}
    if units:
        param_attr["units"] = "mmol_per_gDW_per_hr"
    # the most common bounds are the minimum, maximum, and 0
    if len(cobra_model.reactions) > 0:
        min_value = min(cobra_model.reactions.list_attr("lower_bound"))
        max_value = max(cobra_model.reactions.list_attr("upper_bound"))
    else:
        min_value = -1000
        max_value = 1000

    SubElement(parameter_list, "parameter", value=strnum(min_value),
               id="cobra_default_lb", sboTerm="SBO:0000626", **param_attr)
    SubElement(parameter_list, "parameter", value=strnum(max_value),
               id="cobra_default_ub", sboTerm="SBO:0000626", **param_attr)
    SubElement(parameter_list, "parameter", value="0",
               id="cobra_0_bound", sboTerm="SBO:0000626", **param_attr)

    def create_bound(reaction, bound_type):
        """returns the str id of the appropriate bound for the reaction

        The bound will also be created if necessary"""
        value = getattr(reaction, bound_type)
        if value == min_value:
            return "cobra_default_lb"
        elif value == 0:
            return "cobra_0_bound"
        elif value == max_value:
            return "cobra_default_ub"
        else:
            param_id = "R_" + reaction.id + "_" + bound_type
            SubElement(parameter_list, "parameter", id=param_id,
                       value=strnum(value), sboTerm="SBO:0000625",
                       **param_attr)
            return param_id

    # add in compartments
    compartments_list = SubElement(xml_model, "listOfCompartments")
    compartments = cobra_model.compartments
    for compartment, name in iteritems(compartments):
        SubElement(compartments_list, "compartment", id=compartment, name=name,
                   constant="true")

    # add in metabolites
    species_list = SubElement(xml_model, "listOfSpecies")
    for met in cobra_model.metabolites:
        species = SubElement(species_list, "species",
                             id="M_" + met.id,
                             # Useless required SBML parameters
                             constant="false",
                             boundaryCondition="false",
                             hasOnlySubstanceUnits="false")
        set_attrib(species, "name", met.name)
        annotate_sbml_from_cobra(species, met)
        set_attrib(species, "compartment", met.compartment)
        set_attrib(species, "fbc:charge", met.charge)
        set_attrib(species, "fbc:chemicalFormula", met.formula)

    # add in genes
    if len(cobra_model.genes) > 0:
        genes_list = SubElement(xml_model, GENELIST_TAG)
        for gene in cobra_model.genes:
            gene_id = gene.id.replace(".", SBML_DOT)
            sbml_gene = SubElement(genes_list, GENE_TAG)
            set_attrib(sbml_gene, "fbc:id", "G_" + gene_id)
            name = gene.name
            if name is None or len(name) == 0:
                name = gene.id
            set_attrib(sbml_gene, "fbc:label", gene_id)
            set_attrib(sbml_gene, "fbc:name", name)
            annotate_sbml_from_cobra(sbml_gene, gene)

    # add in reactions
    reactions_list = SubElement(xml_model, "listOfReactions")
    for reaction in cobra_model.reactions:
        id = "R_" + reaction.id
        sbml_reaction = SubElement(
            reactions_list, "reaction",
            id=id,
            # Useless required SBML parameters
            fast="false",
            reversible=str(reaction.lower_bound < 0).lower())
        set_attrib(sbml_reaction, "name", reaction.name)
        annotate_sbml_from_cobra(sbml_reaction, reaction)
        # add in bounds
        set_attrib(sbml_reaction, "fbc:upperFluxBound",
                   create_bound(reaction, "upper_bound"))
        set_attrib(sbml_reaction, "fbc:lowerFluxBound",
                   create_bound(reaction, "lower_bound"))

        # objective coefficient
        if reaction.objective_coefficient != 0:
            objective = SubElement(flux_objectives_list,
                                   ns("fbc:fluxObjective"))
            set_attrib(objective, "fbc:reaction", id)
            set_attrib(objective, "fbc:coefficient",
                       strnum(reaction.objective_coefficient))

        # stoichiometry
        reactants = {}
        products = {}
        for metabolite, stoichiomety in iteritems(reaction._metabolites):
            met_id = "M_" + metabolite.id
            if stoichiomety > 0:
                products[met_id] = strnum(stoichiomety)
            else:
                reactants[met_id] = strnum(-stoichiomety)
        if len(reactants) > 0:
            reactant_list = SubElement(sbml_reaction, "listOfReactants")
            for met_id, stoichiomety in sorted(iteritems(reactants)):
                SubElement(reactant_list, "speciesReference", species=met_id,
                           stoichiometry=stoichiomety, constant="true")
        if len(products) > 0:
            product_list = SubElement(sbml_reaction, "listOfProducts")
            for met_id, stoichiomety in sorted(iteritems(products)):
                SubElement(product_list, "speciesReference", species=met_id,
                           stoichiometry=stoichiomety, constant="true")

        # gene reaction rule
        gpr = reaction.gene_reaction_rule
        if gpr is not None and len(gpr) > 0:
            gpr = gpr.replace(".", SBML_DOT)
            gpr_xml = SubElement(sbml_reaction, GPR_TAG)
            try:
                parsed, _ = parse_gpr(gpr)
                construct_gpr_xml(gpr_xml, parsed.body)
            except Exception as e:
                print("failed on '%s' in %s" %
                      (reaction.gene_reaction_rule, repr(reaction)))
                raise e

    return xml


def _check_required(sbase, value, attribute):
    """ Get required attribute from the SBase.

    :param sbase:
    :param attribute:
    :return:
    """
    if value is None:
        msg = "required attribute '%s' not found in '%s'" % \
              (attribute, sbase)
        if sbase.id is not None:
            msg += " with id '%s'" % sbase.id
        elif sbase.name is not None:
            msg += " with name '%s'" % sbase.get("name")
        raise CobraSBMLError(msg)
    return value


def annotate_cobra_from_sbase(cobj, sbase):
    """ Read annotations from SBase into dictionary.

    :param cobj:
    :param sbase:
    :return:
    """
    annotation = cobj.annotation

    # SBO term
    if sbase.isSetSBOTerm():
        annotation["SBO"] = sbase.getSBOTerm()

    # RDF annotation

    cvterms = sbase.getCVTerms()
    if cvterms is None:
        return

    for cvterm in cvterms:  # type: libsbml.CVTerm
        # FIXME: currently only the terms, but not the qualifier
        # are stored (only subset of identifiers.org parsed)
        for k in range(cvterm.getNumResources()):
            uri = cvterm.getResourceURI(k)
            if not uri.startswith("http://identifiers.org/"):
                warn("%s does not start with http://identifiers.org/" % uri)
                continue
            try:
                provider, identifier = uri[23:].split("/", 1)
            except ValueError:
                warn("%s does not conform to http://identifiers.org/provider/id"
                     % uri)
                continue

            # handle multiple by same provider (create list)
            if provider in annotation:
                if isinstance(annotation[provider], string_types):
                    annotation[provider] = [annotation[provider]]
                annotation[provider].append(identifier)
            else:
                annotation[provider] = identifier


def validate_sbml_model(path):
    """ Validate given SBML model.

    :param path:
    :return:
    """
    assert 0 == 1