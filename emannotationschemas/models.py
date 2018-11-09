from sqlalchemy import Column, String, Integer, Float, Numeric, Boolean, create_engine
from sqlalchemy import ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.declarative import AbstractConcreteBase
from geoalchemy2 import Geometry
from emannotationschemas import get_schema, get_types
from emannotationschemas.base import NumericField, ReferenceAnnotation
from emannotationschemas.contact import Contact
from emannotationschemas.errors import UnknownAnnotationTypeException
import marshmallow as mm
import numpy as np

Base = declarative_base()

root_model_name = "CellSegment"


def get_next_version(sql_uri, dataset_name):
    engine = create_engine(sql_uri)
    versions = np.array([get_table_version(t)
                         for t in engine.table_names() if (dataset_name in t)])
    if len(versions) > 0:
        new_version = np.max(versions)+1
    else:
        new_version = 0
    return new_version


def format_table_name(dataset, table_name, version: int=1):
    return "{}_{}_v{}".format(dataset, table_name, version)


def get_table_version(table_name):
    return int(table_name.split('_')[-1][1:])

class ModelStore():

    def __init__(self):
        self.container = {}

    @staticmethod
    def to_key(dataset, table_name, version: int=1):
        return format_table_name(dataset, table_name, version)

    def contains_model(self, dataset, table_name, version: int=1):
        return self.to_key(dataset, table_name, version) in self.container.keys()

    def get_model(self, dataset, table_name, version: int=1):
        key = self.to_key(dataset, table_name, version)
        return self.container[key]

    def set_model(self, dataset, table_name, model, version: int=1):
        key = self.to_key(dataset, table_name, version)
        self.container[key] = model


annotation_models = ModelStore()

# TODO decide what to call this for real



class InvalidSchemaField(Exception):
    '''Exception raised if a schema can't be translated to a model'''


def validate_types(schemas_and_tables):
    '''normalize a list of desired annotation types
    if passed None returns all types, otherwise checks that types exist
    Parameters
    ----------
    types: list[str] or None

    Returns
    -------
    list[str]
        list of types

    Raises
    ------
    UnknownAnnotationTypeException
        If types contains an invalid type
    '''

    all_types = get_types()
    if not (all(sn in all_types for sn, tn in schemas_and_tables)):
        bad_types = [sn for sn,
                     tn in schemas_and_tables if sn not in all_types]
        msg = '{} are invalid types'.format(bad_types)
        raise UnknownAnnotationTypeException(msg)


def make_dataset_models(dataset, schemas_and_tables, version: int=1, include_contacts=False):
    """make all the models for a dataset

    Parameters
    ----------
    dataset: str
        name of dataset
    table_and_types: list[(schema_name, table_name)]
        list of tuples with types and model names to make
    version: str
        version number to use for making models
    include_contacts:
        option to include the model for cell contacts

    Returns
    -------
    dict
        dictionary where keys are table_names and values are sqlalchemy Models

    Raises
    ------
    UnknownAnnotationTypeException
        If a type is not a valid annotation type
    """

    validate_types(schemas_and_tables)
    dataset_dict = {}
    cell_segment_model = make_cell_segment_model(dataset, version=version)
    dataset_dict[root_model_name.lower()] = cell_segment_model
    for schema_name, table_name in schemas_and_tables:
        model_key = table_name
        dataset_dict[model_key] = make_annotation_model(dataset,
                                                        schema_name,
                                                        table_name,
                                                        version=version)
    if include_contacts:
        contact_model = make_annotation_model_from_schema(dataset,
                                                          'contact',
                                                          Contact,
                                                          version=version)
        dataset_dict['contact'] = contact_model
    return dataset_dict


def make_all_models(datasets, schemas_and_tables=None,
                    version="v1", include_contacts=False):
    """make all the models for a dataset

    Parameters
    ----------
    datasets: list[str]
        list of datasets to make models for
    schemas_and_tables: list[(str, str_]
        list of schema, table names to make
    include_contacts:
        option to include the model for cell contacts

    Returns
    -------
    dict
        2 level nested dictionary where first key is dataset,
        and second key is types and values are sqlalchemy Models


    Raises
    ------
    UnknownAnnotationTypeException
        If a type is not a valid annotation type
    """

    model_dict = {}
    validate_types(schemas_and_tables)
    for dataset in datasets:
        model_dict[dataset] = make_dataset_models(dataset,
                                                  schemas_and_tables,
                                                  include_contacts=include_contacts,
                                                  version=version)
    return model_dict


field_column_map = {
    NumericField: Numeric,
    mm.fields.Int: Integer,
    mm.fields.Integer: Integer,
    mm.fields.Float: Float,
    mm.fields.Str: String,
    mm.fields.String: String,
    mm.fields.Bool: Boolean,
    mm.fields.Boolean: Boolean
}


def add_column(attrd, k, field, dataset, version: int=1):
    field_type = type(field)
    do_index = field.metadata.get('index', False)
    if field_type in field_column_map:
        attrd[k] = Column(field_column_map[field_type], index=do_index)
    else:
        if isinstance(field, mm.fields.Nested):
            if field.many:
                raise InvalidSchemaField("Nested(many=True) not supported")
            for sub_k, sub_field in field.nested._declared_fields.items():
                do_sub_index = sub_field.metadata.get('index', False)
                postgis_geom = sub_field.metadata.get('postgis_geometry',
                                                      None)
                if postgis_geom:
                    attrd[k + "_" + sub_k] = Column(Geometry(postgis_geom,
                                                             dimension=3))
                else:
                    dyn_args = [field_column_map[type(sub_field)]]
                    if sub_k == 'root_id':
                        table_name = format_table_name(dataset,
                                                       root_model_name.lower(),
                                                       version=version)
                        fk = table_name + ".id"
                        dyn_args.append(ForeignKey(fk))
                    attrd[k + "_" +
                          sub_k] = Column(*dyn_args,
                                          index=do_sub_index)
        else:
            raise InvalidSchemaField(
                "field type {} not supported".format(field_type))
    return attrd


def make_cell_segment_model(dataset, version: int=1):
    root_type = root_model_name.lower()
    attr_dict = {
        '__tablename__': format_table_name(dataset, root_type, version=version),
        'id': Column(Numeric, primary_key=True, autoincrement=False)
    }
    model_name = dataset.capitalize() + root_model_name

    if not annotation_models.contains_model(dataset, root_type, version=version):
        annotation_models.set_model(dataset,
                                    root_type,
                                    type(model_name, (Base,), attr_dict),
                                    version=version)
    return annotation_models.get_model(dataset, root_type, version=version)

def declare_annotation_model_from_schema(dataset, table_name, Schema, version: int=1):
    model_name = dataset.capitalize() + table_name.capitalize()
    attrd = {
            '__tablename__': format_table_name(dataset, table_name, version=version),
            '__mapper_args__': {
                'polymorphic_identity': dataset,
                'concrete': True
            },
            'id': Column(Numeric, primary_key=True, autoincrement=False)
        }
    for k, field in Schema._declared_fields.items():
        if (not field.metadata.get('drop_column', False)):
            attrd = add_column(attrd, k, field, dataset, version=version)
    if issubclass(Schema, ReferenceAnnotation):
        target_field = Schema._declared_fields['target_id']
        reference_type = target_field.metadata['reference_type']
        attrd['target_id'] = Column(Integer, ForeignKey(
            dataset + '_' + reference_type + '.id'))
    return type(model_name, (Base,), attrd)
        
def make_annotation_model_from_schema(dataset, table_name, Schema, version: int=1):
    if not annotation_models.contains_model(dataset, table_name, version=version):
        Model = declare_annotation_model_from_schema(dataset, table_name, Schema, version=version)
        annotation_models.set_model(dataset,
                                    table_name,
                                    Model,
                                    version=version)

    return annotation_models.get_model(dataset, table_name, version=version)

def declare_annotation_model(dataset, annotation_type, table_name, version: int=1):
    Schema = get_schema(annotation_type)
    return declare_annotation_model_from_schema(dataset, table_name, Schema, version=version)

def make_annotation_model(dataset, annotation_type, table_name, version: int=1):
    Schema = get_schema(annotation_type)
    return make_annotation_model_from_schema(dataset, table_name, Schema, version=version)
