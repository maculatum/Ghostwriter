# Generated by Django 3.2.19 on 2023-09-18 20:15

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reporting', '0039_merge_20230918_2015'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reporttemplate',
            name='p_style',
            field=models.CharField(blank=True, default=None, help_text='Provide the name of the style of new paragraphs. The style must be present in the template. Leave empty to use default Normal style (Word only).', max_length=255, null=True, verbose_name='Style of new paragraphs - Word only (leave empty for default - Normal)'),
        ),
    ]